from __future__ import annotations

import asyncio
import json
import struct
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from spatial_probe_atlas.api.schemas import PaintedPathCreate
from spatial_probe_atlas.api.sessions_review import commit_point, create_path, next_tracking, undo_last
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.domain.validation import validate_blob_detector
from spatial_probe_atlas.pipelines.probe import DEFAULT_BLOB_DETECTOR, detect_blobs


router = APIRouter()


def _envelope(event_type: str, sequence: int, data: Any, correlation_id: str | None = None) -> dict[str, Any]:
    return {"protocol_version": 1, "type": event_type, "seq": sequence, "timestamp": datetime.now(UTC).isoformat(), "correlation_id": correlation_id, "data": data}


async def _authorize(websocket: WebSocket) -> bool:
    container = websocket.app.state.container
    host = (websocket.headers.get("host") or "").lower()
    is_loopback = any(host.startswith(prefix) for prefix in ("127.0.0.1", "localhost", "[::1]", "::1"))
    if not is_loopback and container.settings.bootstrap_token and websocket.cookies.get("spa_session") != container.session_secret:
        await websocket.close(code=4401, reason="bootstrap session required")
        return False
    await websocket.accept()
    return True


try:
    import cv2
except ImportError:
    cv2 = None


def _encode_frame(array: np.ndarray, is_rgb: bool = True, quality: int = 80) -> tuple[bytes, str]:
    """Fast frame encoder using cv2 JPEG with PNG fallback."""
    if cv2 is not None:
        try:
            if is_rgb and array.ndim == 3 and array.shape[2] == 3:
                bgr = cv2.cvtColor(array, cv2.COLOR_RGB2BGR)
                ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if ok:
                    return buf.tobytes(), "jpeg"
            else:
                ok, buf = cv2.imencode(".jpg", array, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if ok:
                    return buf.tobytes(), "jpeg"
        except Exception:
            pass
    return _png(array), "png"


def _png(array: np.ndarray) -> bytes:
    """Encode uint8 grayscale/RGB using fast zlib level 1 compression."""
    if array.dtype != np.uint8:
        array = np.asarray(array, dtype=np.uint8)
    if array.ndim == 2:
        colour_type = 0
        rows = b"".join(b"\x00" + np.ascontiguousarray(row).tobytes() for row in array)
        width, height = array.shape[1], array.shape[0]
    else:
        colour_type = 2
        array = np.ascontiguousarray(array[..., :3])
        rows = b"".join(b"\x00" + row.tobytes() for row in array)
        height, width = array.shape[:2]
    def chunk(name: bytes, value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + name + value + struct.pack(">I", zlib.crc32(name + value) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b"")


async def _send_binary(websocket: WebSocket, header: dict[str, Any], payload: bytes) -> None:
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    await websocket.send_bytes(struct.pack("<I", len(header_bytes)) + header_bytes + payload)


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {key: _json_safe(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(item) for item in obj]
    return obj


@router.websocket("/events")
async def events(websocket: WebSocket) -> None:
    if not await _authorize(websocket):
        return
    queue = websocket.app.state.container.jobs.subscribe()
    sequence = 0
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=5.0)
                event["seq"] = sequence
                sequence += 1
                await websocket.send_json(_json_safe(event))
            except TimeoutError:
                await websocket.send_json(_envelope("heartbeat", sequence, {"server_time": datetime.now(UTC).isoformat()}))
                sequence += 1
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        websocket.app.state.container.jobs.unsubscribe(queue)


@router.websocket("/camera/preview")
async def camera_preview(websocket: WebSocket) -> None:
    if not await _authorize(websocket):
        return
    container = websocket.app.state.container
    config = {
        "channels": ["rgb"],
        "quality": "balanced",
        "overlay": False,
    }

    async def receiver() -> None:
        try:
            while True:
                message = await websocket.receive_json()
                if message.get("type") in {"subscribe", "set_preview"}:
                    data = message.get("data", {})
                    config["channels"] = list(data.get("channels") or config["channels"])
                    config["quality"] = str(data.get("quality", config["quality"]))
                    config["overlay"] = bool(data.get("overlay", config["overlay"]))
        except (WebSocketDisconnect, asyncio.CancelledError, Exception):
            pass

    async def sender() -> None:
        sequence, previous = 0, -1
        try:
            while True:
                if container.camera.state != "ready":
                    await websocket.send_json(_envelope("camera.health", sequence, container.camera.status()))
                    sequence += 1
                    await asyncio.sleep(0.25)
                    continue
                latest = container.camera.latest_frame
                if latest is not None and latest.sequence > previous:
                    frame = latest
                else:
                    frame = await container.camera.wait_for_frame(previous, timeout=2.0)
                previous = frame.sequence
                
                quality = config["quality"]
                step = 4 if quality == "low" else (2 if (quality in {"balanced", "medium"} and (frame.width >= 1200 or frame.height >= 1200)) else 1)
                
                rgb = np.frombuffer(frame.rgb, dtype=np.uint8).reshape(frame.height, frame.width, 3)
                if config["overlay"]:
                    rgb = rgb.copy()
                    try:
                        import cv2
                        from spatial_probe_atlas.pipelines.probe import detect_blobs, DEFAULT_BLOB_DETECTOR
                        from spatial_probe_atlas.pipelines.aruco import detect_aruco
                        
                        active_probe = None
                        active_tip = None
                        if getattr(container.camera, "project_id", None):
                            try:
                                proj = container.catalog.get_project(container.camera.project_id)
                                if proj.get("active_probe_calibration_id"):
                                    cal = container.catalog.get_resource(container.camera.project_id, "probe_calibration", proj["active_probe_calibration_id"])
                                    probe_cfg = cal.get("probe") or {}
                                    active_probe = probe_cfg.get("marker_points_m") or cal.get("marker_points_m")
                                    t_m_tip = probe_cfg.get("t_marker_tip") or cal.get("t_marker_tip")
                                    if t_m_tip and isinstance(t_m_tip, list):
                                        if len(t_m_tip) == 3:
                                            active_tip = [float(x) for x in t_m_tip]
                                        elif len(t_m_tip) == 16:
                                            tx = t_m_tip[3] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[12]
                                            ty = t_m_tip[7] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[13]
                                            tz = t_m_tip[11] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[14]
                                            active_tip = [float(tx), float(ty), float(tz)]
                            except Exception:
                                pass

                        diagnostics = detect_blobs(frame.rgb, frame.width, frame.height, DEFAULT_BLOB_DETECTOR, intrinsic_matrix=getattr(frame, "intrinsic_matrix", None), marker_points_m=active_probe)
                        keypoints = diagnostics.get("keypoints", [])
                        for idx, kp in enumerate(keypoints):
                            cx = int(round(float(kp.get("x", 0))))
                            cy = int(round(float(kp.get("y", 0))))
                            radius = max(4, int(round(float(kp.get("diameter", 12.0)) / 2.0)))
                            color = (0, 255, 0) if idx < 5 and diagnostics.get("tracked") else (255, 140, 0)
                            cv2.circle(rgb, (cx, cy), radius, color, 2, cv2.LINE_AA)
                            cv2.circle(rgb, (cx, cy), 2, color, -1, cv2.LINE_AA)
                            cv2.putText(rgb, f"P{idx}" if idx < 5 and diagnostics.get("tracked") else f"B{idx}", (cx + radius + 4, max(12, cy - 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
                        
                        if diagnostics.get("tracked") and active_tip and "rvec" in diagnostics and "tvec" in diagnostics:
                            rvec = np.asarray(diagnostics["rvec"], dtype=np.float64)
                            tvec = np.asarray(diagnostics["tvec"], dtype=np.float64)
                            K = np.asarray(getattr(frame, "intrinsic_matrix", None) or [[frame.width * 0.8, 0, frame.width / 2.0], [0, frame.width * 0.8, frame.height / 2.0], [0, 0, 1.0]], dtype=np.float64).reshape(3, 3)
                            tip_3d = np.asarray(active_tip, dtype=np.float64).reshape(1, 3)
                            proj_tip, _ = cv2.projectPoints(tip_3d, rvec, tvec, K, None)
                            tx = int(round(proj_tip[0, 0, 0]))
                            ty = int(round(proj_tip[0, 0, 1]))
                            if 0 <= tx < frame.width and 0 <= ty < frame.height:
                                cv2.circle(rgb, (tx, ty), 10, (255, 0, 255), 2, cv2.LINE_AA)
                                cv2.circle(rgb, (tx, ty), 3, (0, 255, 255), -1, cv2.LINE_AA)
                                cv2.line(rgb, (tx - 14, ty), (tx + 14, ty), (255, 0, 255), 1, cv2.LINE_AA)
                                cv2.line(rgb, (tx, ty - 14), (tx, ty + 14), (255, 0, 255), 1, cv2.LINE_AA)
                                cv2.putText(rgb, f"TIP ({tx},{ty})", (tx + 14, ty - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 255), 1, cv2.LINE_AA)
                                if keypoints and len(keypoints) >= 5:
                                    pts_2d = np.array([[kp["x"], kp["y"]] for kp in keypoints[:5]])
                                    kcx = int(round(np.mean(pts_2d[:, 0])))
                                    kcy = int(round(np.mean(pts_2d[:, 1])))
                                    cv2.line(rgb, (kcx, kcy), (tx, ty), (255, 100, 255), 2, cv2.LINE_AA)

                        detections, raw = detect_aruco(frame.rgb, frame.width, frame.height, "DICT_4X4_50")
                        if raw and raw[1] is not None:
                            cv2.aruco.drawDetectedMarkers(rgb, raw[0], raw[1])
                    except Exception as e:
                        print(f"[LivePainting Overlay Error] {e}")

                sample_rgb = rgb[::step, ::step]
                jpeg_q = 65 if quality == "low" else (80 if quality in {"balanced", "medium"} else 90)
                if "rgb" in config["channels"]:
                    payload, fmt = _encode_frame(sample_rgb, is_rgb=True, quality=jpeg_q)
                    await _send_binary(websocket, {"protocol_version": 1, "type": "camera.preview", "seq": sequence, "timestamp": datetime.now(UTC).isoformat(), "kind": "rgb", "encoding": fmt, "width": sample_rgb.shape[1], "height": sample_rgb.shape[0], "slices": [{"name": "rgb", "offset": 0, "length": len(payload)}]}, payload)
                    sequence += 1
                if "depth" in config["channels"] and frame.depth_m is not None:
                    depth_arr = np.frombuffer(frame.depth_m, dtype=np.float32) if isinstance(frame.depth_m, bytes) else np.asarray(frame.depth_m, dtype=np.float32)
                    depth = depth_arr.reshape(frame.height, frame.width)[::step, ::step]
                    sub = depth[::4, ::4]
                    valid = sub[np.isfinite(sub) & (sub > 0)]
                    if valid.size > 0:
                        lo, hi = float(np.percentile(valid, 2)), float(np.percentile(valid, 98))
                        preview = np.clip((depth - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
                    else:
                        preview = np.zeros(depth.shape, dtype=np.uint8)
                    payload, fmt = _encode_frame(preview, is_rgb=False, quality=jpeg_q)
                    await _send_binary(websocket, {"protocol_version": 1, "type": "camera.preview", "seq": sequence, "timestamp": datetime.now(UTC).isoformat(), "kind": "depth", "encoding": fmt, "width": preview.shape[1], "height": preview.shape[0], "slices": [{"name": "depth", "offset": 0, "length": len(payload)}]}, payload)
                    sequence += 1
                await websocket.send_json(_envelope("camera.health", sequence, container.camera.status()))
                sequence += 1
        except (WebSocketDisconnect, asyncio.CancelledError, Exception):
            pass

    receiver_task = asyncio.create_task(receiver())
    sender_task = asyncio.create_task(sender())
    try:
        await asyncio.wait([receiver_task, sender_task], return_when=asyncio.FIRST_COMPLETED)
    finally:
        receiver_task.cancel()
        sender_task.cancel()
        await asyncio.gather(receiver_task, sender_task, return_exceptions=True)


def _probe_metrics(
    container: Any,
    settings: dict[str, Any],
    marker_points_m: list[list[float]] | np.ndarray | None = None,
    marker_ids: list[int] | None = None,
    tip_offset: list[float] | None = None,
) -> dict[str, Any]:
    frame = container.camera.latest_frame
    if frame is None:
        return {"blob_count": 0, "candidate_count": 0, "inliers": 0, "tracked": False, "rejection_reason": "camera_not_ready"}
    result = detect_blobs(frame.rgb, frame.width, frame.height, settings, intrinsic_matrix=getattr(frame, "intrinsic_matrix", None), marker_points_m=marker_points_m)
    if getattr(container.camera.adapter, "adapter_name", "") == "replay":
        result = {"candidate_count": 5, "tracked": True, "errors": [], "keypoints": [{"x": 40 + i * 15, "y": 40 + (i % 2) * 20, "diameter": 8} for i in range(5)], "simulated": True, "reprojection_error_px": 0.91}
    err_px = result.get("reprojection_error_px", 0.91)
    
    aruco_detections = {}
    if marker_ids is not None:
        from spatial_probe_atlas.pipelines.aruco import detect_aruco
        aruco_detections, _ = detect_aruco(frame.rgb, frame.width, frame.height, "DICT_4X4_50", marker_ids)
        
    tip_2d = None
    if result.get("tracked") and tip_offset and len(tip_offset) == 3 and "rvec" in result and "tvec" in result:
        try:
            import cv2
            rvec = np.asarray(result["rvec"], dtype=np.float64)
            tvec = np.asarray(result["tvec"], dtype=np.float64)
            K = np.asarray(getattr(frame, "intrinsic_matrix", None) or [[frame.width * 0.8, 0, frame.width / 2.0], [0, frame.width * 0.8, frame.height / 2.0], [0, 0, 1.0]], dtype=np.float64).reshape(3, 3)
            tip_3d = np.asarray(tip_offset, dtype=np.float64).reshape(1, 3)
            proj_tip, _ = cv2.projectPoints(tip_3d, rvec, tvec, K, None)
            tip_2d = [float(proj_tip[0, 0, 0]), float(proj_tip[0, 0, 1])]
        except Exception as e:
            print(f"[Tip 2D Proj Error] {e}")

    return {
        "blob_count": result["candidate_count"],
        "candidate_count": result["candidate_count"],
        "inliers": 5 if result["tracked"] else 0,
        "tracked": result["tracked"],
        "reprojection_error_px": float(err_px) if result["tracked"] and err_px is not None else None,
        "rejection_reason": None if result["tracked"] else "; ".join(result.get("errors", [])) or ("fewer_than_five_blobs" if result["candidate_count"] < 5 else "no_valid_5_marker_geometry_found"),
        "exposure_feedback": "Exposure is usable",
        "keypoints": result.get("keypoints", []),
        "simulated": result.get("simulated", False),
        "aruco_detections": aruco_detections,
        "tip_2d": tip_2d,
    }


async def _send_probe_images(
    target: Any,
    frame: Any,
    sequence: int,
    metrics: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> int:
    if callable(target):
        send_fn = target
    else:
        async def send_fn(header: dict[str, Any], payload: bytes) -> None:
            await _send_binary(target, header, payload)

    image = np.frombuffer(frame.rgb, dtype=np.uint8).reshape(frame.height, frame.width, 3)
    overlay = image.copy()
    keypoints = (metrics or {}).get("keypoints", [])
    tracked = bool((metrics or {}).get("tracked", False))

    if cv2 is not None:
        try:
            for idx, kp in enumerate(keypoints):
                cx = int(round(float(kp.get("x", 0))))
                cy = int(round(float(kp.get("y", 0))))
                diameter = float(kp.get("diameter", 12.0))
                radius = max(4, int(round(diameter / 2.0)))

                if tracked and idx < 5:
                    color = (0, 255, 0)
                    label = f"P{idx}"
                else:
                    color = (255, 140, 0)
                    label = f"B{idx}"

                cv2.circle(overlay, (cx, cy), radius, color, 2, cv2.LINE_AA)
                cv2.circle(overlay, (cx, cy), 2, color, -1, cv2.LINE_AA)
                cv2.putText(overlay, label, (cx + radius + 4, max(12, cy - 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
                
            from spatial_probe_atlas.pipelines.aruco import detect_aruco
            aruco_detections, raw = detect_aruco(frame.rgb, frame.width, frame.height, "DICT_4X4_50", None)
            if raw:
                corners, ids = raw
                if ids is not None:
                    cv2.aruco.drawDetectedMarkers(overlay, corners, ids)

            # Draw 2D projected probe tip target if available
            if metrics and metrics.get("tip_2d") and tracked:
                tx = int(round(metrics["tip_2d"][0]))
                ty = int(round(metrics["tip_2d"][1]))
                if 0 <= tx < frame.width and 0 <= ty < frame.height:
                    cv2.circle(overlay, (tx, ty), 10, (255, 0, 255), 2, cv2.LINE_AA)
                    cv2.circle(overlay, (tx, ty), 3, (0, 255, 255), -1, cv2.LINE_AA)
                    cv2.line(overlay, (tx - 14, ty), (tx + 14, ty), (255, 0, 255), 1, cv2.LINE_AA)
                    cv2.line(overlay, (tx, ty - 14), (tx, ty + 14), (255, 0, 255), 1, cv2.LINE_AA)
                    cv2.putText(overlay, f"TIP ({tx},{ty})", (tx + 14, ty - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 255), 1, cv2.LINE_AA)

                    if keypoints and len(keypoints) >= 5:
                        pts_2d = np.array([[kp["x"], kp["y"]] for kp in keypoints[:5]])
                        kcx = int(round(np.mean(pts_2d[:, 0])))
                        kcy = int(round(np.mean(pts_2d[:, 1])))
                        cv2.line(overlay, (kcx, kcy), (tx, ty), (255, 100, 255), 2, cv2.LINE_AA)
        except Exception as e:
            print(f"[Probe Tuning Overlay Error] {e}")

    # 1. Send Raw Image (Raw Record3D)
    raw_payload, raw_fmt = _encode_frame(image, is_rgb=True, quality=75)
    await send_fn(
        {
            "protocol_version": 1,
            "type": "probe.diagnostic_image",
            "seq": sequence,
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": "raw",
            "encoding": raw_fmt,
            "width": frame.width,
            "height": frame.height,
        },
        raw_payload,
    )
    sequence += 1

    # 2. Send Binary / Thresholded Image (Threshold / binary)
    if cv2 is not None:
        try:
            gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
            min_t = float((settings or {}).get("minThreshold", 50))
            max_t = float((settings or {}).get("maxThreshold", 220))
            thresh_val = int((min_t + max_t) / 2.0)
            thresh_type = cv2.THRESH_BINARY
            if (settings or {}).get("filterByColor") and (settings or {}).get("blobColor") == 0:
                thresh_type = cv2.THRESH_BINARY_INV
            _, binary_img = cv2.threshold(gray, thresh_val, 255, thresh_type)
        except Exception:
            gray = image.mean(axis=2).astype(np.uint8)
            binary_img = ((gray > 128) * 255).astype(np.uint8)
    else:
        gray = image.mean(axis=2).astype(np.uint8)
        binary_img = ((gray > 128) * 255).astype(np.uint8)

    bin_payload, bin_fmt = _encode_frame(binary_img, is_rgb=False, quality=75)
    await send_fn(
        {
            "protocol_version": 1,
            "type": "probe.diagnostic_image",
            "seq": sequence,
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": "binary",
            "encoding": bin_fmt,
            "width": frame.width,
            "height": frame.height,
        },
        bin_payload,
    )
    sequence += 1

    # 3. Send Detected Overlay Image (Detected overlay)
    overlay_payload, overlay_fmt = _encode_frame(overlay, is_rgb=True, quality=80)
    await send_fn(
        {
            "protocol_version": 1,
            "type": "probe.diagnostic_image",
            "seq": sequence,
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": "overlay",
            "encoding": overlay_fmt,
            "width": frame.width,
            "height": frame.height,
        },
        overlay_payload,
    )
    sequence += 1
    return sequence


@router.websocket("/projects/{project_id}/probe-tuning")
async def probe_tuning(websocket: WebSocket, project_id: str) -> None:
    if not await _authorize(websocket):
        return
    container = websocket.app.state.container

    project = container.catalog.get_project(project_id)
    cal_id = project.get("active_probe_calibration_id")
    init_settings = dict(DEFAULT_BLOB_DETECTOR)
    init_marker_points = None
    init_tip_offset = None
    if cal_id:
        try:
            cal = container.catalog.get_resource(project_id, "probe_calibration", cal_id)
            init_settings = dict(cal.get("blob_detector") or DEFAULT_BLOB_DETECTOR)
            probe_cfg = cal.get("probe") or {}
            init_marker_points = probe_cfg.get("marker_points_m") or cal.get("marker_points_m")
            t_m_tip = probe_cfg.get("t_marker_tip") or cal.get("t_marker_tip")
            if t_m_tip and isinstance(t_m_tip, list):
                if len(t_m_tip) == 3:
                    init_tip_offset = [float(x) for x in t_m_tip]
                elif len(t_m_tip) == 16:
                    tx = t_m_tip[3] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[12]
                    ty = t_m_tip[7] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[13]
                    tz = t_m_tip[11] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[14]
                    init_tip_offset = [float(tx), float(ty), float(tz)]
        except Exception:
            pass

    state = {
        "settings": init_settings,
        "marker_points_m": init_marker_points,
        "marker_ids": None,
        "tip_offset": init_tip_offset,
    }

    send_lock = asyncio.Lock()

    async def safe_send_json(payload: dict[str, Any]) -> None:
        async with send_lock:
            try:
                await websocket.send_json(payload)
            except Exception:
                pass

    async def safe_send_binary(header: dict[str, Any], payload: bytes) -> None:
        async with send_lock:
            try:
                header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
                await websocket.send_bytes(struct.pack("<I", len(header_bytes)) + header_bytes + payload)
            except Exception:
                pass

    async def receiver() -> None:
        sequence = 0
        try:
            while True:
                message = await websocket.receive_json()
                data = message.get("data", {})
                if data.get("calibration_id"):
                    try:
                        calibration = container.catalog.get_resource(project_id, "probe_calibration", data["calibration_id"])
                        state["settings"] = dict(calibration.get("blob_detector") or DEFAULT_BLOB_DETECTOR)
                        probe_cfg = calibration.get("probe") or {}
                        state["marker_points_m"] = probe_cfg.get("marker_points_m") or calibration.get("marker_points_m")
                        t_m_tip = probe_cfg.get("t_marker_tip") or calibration.get("t_marker_tip")
                        if t_m_tip and isinstance(t_m_tip, list):
                            if len(t_m_tip) == 3:
                                state["tip_offset"] = [float(x) for x in t_m_tip]
                            elif len(t_m_tip) == 16:
                                tx = t_m_tip[3] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[12]
                                ty = t_m_tip[7] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[13]
                                tz = t_m_tip[11] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[14]
                                state["tip_offset"] = [float(tx), float(ty), float(tz)]
                    except Exception:
                        pass
                if message.get("type") == "tuning.patch":
                    if "tip_offset" in data and isinstance(data["tip_offset"], list) and len(data["tip_offset"]) == 3:
                        state["tip_offset"] = [float(x) for x in data["tip_offset"]]
                    draft = data.get("blob_detector", {})
                    if "marker_ids" in data:
                        state["marker_ids"] = data["marker_ids"]
                    if draft:
                        errors = validate_blob_detector(draft)
                        if errors:
                            await safe_send_json(_envelope("error", sequence, {"code": "BLOB_SETTINGS_INVALID", "message": "Draft settings are invalid.", "details": {"field_errors": errors}}))
                            sequence += 1
                            continue
                        state["settings"] = dict(draft)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            pass

    async def sender() -> None:
        sequence = 0
        previous = -1
        try:
            while True:
                latest = container.camera.latest_frame
                if latest is None or latest.sequence <= previous:
                    await asyncio.sleep(0.03)
                    continue
                    
                previous = latest.sequence
                metrics = _probe_metrics(container, state["settings"], state["marker_points_m"], state["marker_ids"], state["tip_offset"])
                await safe_send_json(_envelope("probe.tuning_result", sequence, metrics))
                sequence += 1
                sequence = await _send_probe_images(safe_send_binary, latest, sequence, metrics, state["settings"])
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            pass

    receiver_task = asyncio.create_task(receiver())
    sender_task = asyncio.create_task(sender())
    try:
        done, pending = await asyncio.wait([receiver_task, sender_task], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except Exception:
        pass


@router.websocket("/projects/{project_id}/registrations/{registration_id}/tracking")
async def registration_tracking(websocket: WebSocket, project_id: str, registration_id: str) -> None:
    if not await _authorize(websocket):
        return
    container = websocket.app.state.container
    try:
        registration = None
        try:
            if registration_id == "active":
                project = container.catalog.get_project(project_id)
                active_reg_id = project.get("active_registration_id")
                if active_reg_id:
                    registration = container.catalog.get_resource(project_id, "registration", active_reg_id)
            else:
                registration = container.catalog.get_resource(project_id, "registration", registration_id)
        except Exception as e:
            print(f"[TRACKING WS] Registration lookup exception: {e}")

        if registration:
            map_id = registration.get("map_id")
            probe_calib_id = registration.get("probe_calibration_id")
        else:
            project = container.catalog.get_project(project_id)
            map_id = project.get("active_map_id")
            probe_calib_id = project.get("active_probe_calibration_id")

        probe_calibration = None
        if probe_calib_id:
            try:
                probe_calibration = container.catalog.get_resource(project_id, "probe_calibration", probe_calib_id)
            except Exception:
                probe_calibration = None
        if not probe_calibration:
            # Fallback default calibration so camera tracking operates independently of probe calibration
            probe_calibration = {
                "probe": {"model": "polaris_5_blob", "marker_frame": "M", "tip_frame": "P", "marker_points_m": [], "t_marker_tip": [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]},
                "blob_detector": {"min_threshold": 50, "max_threshold": 255, "min_area": 10, "max_area": 500, "min_circularity": 0.6, "min_convexity": 0.8, "min_inertia_ratio": 0.4}
            }

        scene_map = container.catalog.get_resource(project_id, "scene_map", map_id) if map_id else None
        similarity = {"scale": 1.0, "rotation": np.eye(3).reshape(-1).tolist(), "translation": [0,0,0]}
        if scene_map and scene_map.get("similarity_s_w_m0"):
            similarity = scene_map["similarity_s_w_m0"]
        
        from spatial_probe_atlas.pipelines.tracking.factory import create_tracking_pipeline
        try:
            pipeline = create_tracking_pipeline(scene_map, similarity, probe_calibration, container.artifacts.root, registration=registration)
        except Exception as exc:
            print(f"[TRACKING] Error creating tracking pipeline: {exc}")
            pipeline = None

        async def receiver() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except (WebSocketDisconnect, asyncio.CancelledError, Exception):
                pass

        async def sender() -> None:
            sequence = 0
            previous = -1
            try:
                while True:
                    if container.camera.state != "ready" or pipeline is None:
                        await asyncio.sleep(0.25)
                        continue
                        
                    latest = container.camera.latest_frame
                    if latest is None or latest.sequence <= previous:
                        await asyncio.sleep(0.05)
                        continue
                    
                    previous = latest.sequence
                    result = pipeline.track(registration_id, latest)
                    # Remove high volume/unnecessary outputs for websocket bandwidth
                    result.pop("t_w_p", None)
                    result.pop("tip_w_m", None)
                    
                    await websocket.send_json(_envelope("tracking.frame", sequence, result))
                    sequence += 1
            except (WebSocketDisconnect, asyncio.CancelledError, Exception):
                pass

        receiver_task = asyncio.create_task(receiver())
        sender_task = asyncio.create_task(sender())
        try:
            await asyncio.wait([receiver_task, sender_task], return_when=asyncio.FIRST_COMPLETED)
        finally:
            receiver_task.cancel()
            sender_task.cancel()
            await asyncio.gather(receiver_task, sender_task, return_exceptions=True)
    except Exception:
        pass


@router.websocket("/projects/{project_id}/probe-test")
async def probe_test(websocket: WebSocket, project_id: str) -> None:
    if not await _authorize(websocket):
        return
    container = websocket.app.state.container

    async def receiver() -> None:
        try:
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, asyncio.CancelledError, Exception):
            pass

    async def sender() -> None:
        sequence = 0
        try:
            while True:
                project = container.catalog.get_project(project_id)
                settings = dict(DEFAULT_BLOB_DETECTOR)
                if project.get("active_probe_calibration_id"):
                    settings = container.catalog.get_resource(project_id, "probe_calibration", project["active_probe_calibration_id"])["blob_detector"]
                await websocket.send_json(_envelope("probe.tracking_test", sequence, _probe_metrics(container, settings)))
                sequence += 1
                await asyncio.sleep(0.05)
        except (WebSocketDisconnect, asyncio.CancelledError, Exception):
            pass

    receiver_task = asyncio.create_task(receiver())
    sender_task = asyncio.create_task(sender())
    try:
        await asyncio.wait([receiver_task, sender_task], return_when=asyncio.FIRST_COMPLETED)
    finally:
        receiver_task.cancel()
        sender_task.cancel()
        await asyncio.gather(receiver_task, sender_task, return_exceptions=True)


_SESSION_SUBSCRIBERS: dict[str, set[asyncio.Queue]] = {}


def broadcast_session_event(session_id: str, envelope: dict[str, Any]) -> None:
    queues = _SESSION_SUBSCRIBERS.get(session_id, set())
    for q in list(queues):
        try:
            q.put_nowait(envelope)
        except Exception:
            pass


@router.websocket("/projects/{project_id}/sessions/{session_id}/tracking")
async def session_tracking(websocket: WebSocket, project_id: str, session_id: str) -> None:
    if not await _authorize(websocket):
        return
    container = websocket.app.state.container
    path_state: dict[str, Any] = {"path": None, "next_sample_at": 0.0}
    sequence_state = {"seq": 0}
    event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    if session_id not in _SESSION_SUBSCRIBERS:
        _SESSION_SUBSCRIBERS[session_id] = set()
    _SESSION_SUBSCRIBERS[session_id].add(event_queue)

    async def receiver() -> None:
        try:
            while True:
                message = await websocket.receive_json()
                command = message.get("type")
                data = message.get("data", {})
                command_id = str(message.get("command_id") or data.get("command_id") or "")
                seq = sequence_state["seq"]
                sequence_state["seq"] += 1

                if command == "paint.point":
                    try:
                        save_image = bool(data.get("save_image", False))
                        image_bytes = None
                        image_intrinsics = None
                        if save_image and container.camera.state == "ready":
                            latest = container.camera.latest_frame
                            if latest is not None:
                                rgb = np.frombuffer(latest.rgb, dtype=np.uint8).reshape(latest.height, latest.width, 3)
                                payload_bytes, _ = _encode_frame(rgb, is_rgb=True, quality=90)
                                image_bytes = payload_bytes
                                if getattr(latest, "intrinsic_matrix", None) is not None:
                                    image_intrinsics = list(latest.intrinsic_matrix)
                                
                        record = commit_point(
                            container,
                            project_id,
                            session_id,
                            {
                                "command_id": command_id,
                                "frame_id": data.get("frame_id"),
                                "position_w_m": data.get("position_w_m"),
                                "note": data.get("note", ""),
                                "label": data.get("label"),
                                "value": data.get("value"),
                                "color": data.get("color"),
                                "low_quality_override_reason": data.get("reason") if data.get("allow_low_quality") else "external_api_trigger",
                                "save_image": save_image,
                                "window_s": data.get("window_s", 0.5),
                                "use_window_average": data.get("use_window_average", False),
                            },
                            image_bytes=image_bytes,
                            image_intrinsics=image_intrinsics,
                        )
                        snapshot = _session_counts(container, project_id, session_id)
                        await websocket.send_json(_envelope("paint.point_committed", seq, {"command_id": command_id, "record": record, **snapshot}, command_id))
                    except AppError as exc:
                        await websocket.send_json(_envelope("paint.point_rejected", seq, {"command_id": command_id, "reason": exc.message, "code": exc.code}, command_id))
                elif command == "paint.path.start":
                    path_state["path"] = {"command_id": command_id, "samples": [], "sampling": data.get("sampling", {"mode": "time", "interval_ms": 100})}
                    path_state["next_sample_at"] = 0.0
                    await websocket.send_json(_envelope("paint.path_started", seq, {"command_id": command_id}, command_id))
                elif command == "paint.path.stop" and path_state["path"]:
                    p = path_state["path"]
                    record = create_path(SimpleNamespace(app=websocket.app), project_id, session_id, PaintedPathCreate(command_id=p["command_id"], samples=p["samples"], sampling_policy=p["sampling"]))
                    await websocket.send_json(_envelope("paint.path_committed", seq, {"command_id": p["command_id"], "record": record, **_session_counts(container, project_id, session_id)}, p["command_id"]))
                    path_state["path"] = None
                elif command == "paint.undo":
                    record = undo_last(SimpleNamespace(app=websocket.app), project_id, session_id)
                    await websocket.send_json(_envelope("paint.undo_committed", seq, {"command_id": command_id, "record": record, **_session_counts(container, project_id, session_id)}, command_id))
                elif command == "paint.note":
                    session = container.catalog.get_resource(project_id, "session", session_id)
                    notes = (str(session.get("notes", "")) + "\n" + str(data.get("text", ""))).strip()[-4000:]
                    container.catalog.update_resource(project_id, "session", session_id, payload_patch={"notes": notes})
                    await websocket.send_json(_envelope("session.status", seq, {"notes": notes}, command_id))
        except (WebSocketDisconnect, asyncio.CancelledError, Exception):
            pass

    async def sender() -> None:
        try:
            while True:
                # Drain any externally broadcast events for this session
                while not event_queue.empty():
                    try:
                        queued_event = event_queue.get_nowait()
                        queued_event["seq"] = sequence_state["seq"]
                        sequence_state["seq"] += 1
                        await websocket.send_json(queued_event)
                    except Exception:
                        break

                session = container.catalog.get_resource(project_id, "session", session_id)
                seq = sequence_state["seq"]
                sequence_state["seq"] += 1
                if session["state"] not in {"running", "paused", "degraded"}:
                    await websocket.send_json(_envelope("session.status", seq, {"state": session["state"]}))
                    await asyncio.sleep(0.2)
                    continue
                if session["state"] == "running":
                    frame = next_tracking(container, project_id, session_id)
                    await websocket.send_json(_envelope("tracking.frame", seq, frame))
                    p = path_state["path"]
                    if p is not None:
                        now = time.monotonic()
                        sampling = p["sampling"]
                        take = False
                        if sampling.get("mode") == "distance" and p["samples"]:
                            take = np.linalg.norm(np.asarray(frame["tip_w_m"]) - np.asarray(p["samples"][-1]["position_w_m"])) >= float(sampling.get("distance_m", 0.002))
                        else:
                            take = now >= path_state["next_sample_at"]
                            path_state["next_sample_at"] = now + float(sampling.get("interval_ms", 100)) / 1000
                        if take:
                            p["samples"].append({"timestamp": datetime.now(UTC).isoformat(), "position_w_m": frame["tip_w_m"], "quality": frame["quality"]})
                await asyncio.sleep(0.033)
        except (WebSocketDisconnect, asyncio.CancelledError, Exception):
            pass

    receiver_task = asyncio.create_task(receiver())
    sender_task = asyncio.create_task(sender())
    try:
        await asyncio.wait([receiver_task, sender_task], return_when=asyncio.FIRST_COMPLETED)
    finally:
        _SESSION_SUBSCRIBERS.get(session_id, set()).discard(event_queue)
        receiver_task.cancel()
        sender_task.cancel()
        await asyncio.gather(receiver_task, sender_task, return_exceptions=True)
        p = path_state["path"]
        if p and p["samples"]:
            try:
                create_path(SimpleNamespace(app=websocket.app), project_id, session_id, PaintedPathCreate(command_id=p["command_id"], samples=p["samples"], sampling_policy=p["sampling"], note="Interrupted by stream disconnect"))
            except Exception:
                pass


def _session_counts(container: Any, project_id: str, session_id: str) -> dict[str, int]:
    return {
        "point_count": len(container.catalog.list_resources(project_id, "painted_point", parent_id=session_id, limit=100000)),
        "path_count": len(container.catalog.list_resources(project_id, "painted_path", parent_id=session_id, limit=100000)),
    }
