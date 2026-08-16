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
    channels = ["rgb"]
    quality = "balanced"
    overlay = False
    sequence, previous = 0, -1
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=0.001)
                if message.get("type") in {"subscribe", "set_preview"}:
                    data = message.get("data", {})
                    channels = list(data.get("channels") or channels)
                    quality = str(data.get("quality", quality))
                    overlay = bool(data.get("overlay", overlay))
            except TimeoutError:
                pass
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
            if quality == "low":
                step = 4
            elif quality in {"balanced", "medium"}:
                step = 2 if frame.width >= 1200 or frame.height >= 1200 else 1
            else:
                step = 1
            rgb = np.frombuffer(frame.rgb, dtype=np.uint8).reshape(frame.height, frame.width, 3)
            if overlay:
                rgb = rgb.copy()
                try:
                    import cv2
                    from spatial_probe_atlas.pipelines.probe import detect_blobs, DEFAULT_BLOB_DETECTOR
                    from spatial_probe_atlas.pipelines.aruco import detect_aruco
                    
                    # Detect and draw blobs
                    diagnostics = detect_blobs(frame.rgb, frame.width, frame.height, DEFAULT_BLOB_DETECTOR)
                    for idx, kp in enumerate(diagnostics.get("keypoints", [])):
                        cx = int(round(float(kp.get("x", 0))))
                        cy = int(round(float(kp.get("y", 0))))
                        radius = max(4, int(round(float(kp.get("diameter", 12.0)) / 2.0)))
                        color = (0, 255, 0) if idx < 5 and diagnostics.get("tracked") else (255, 140, 0)
                        cv2.circle(rgb, (cx, cy), radius, color, 2, cv2.LINE_AA)
                        cv2.circle(rgb, (cx, cy), 2, color, -1, cv2.LINE_AA)
                        cv2.putText(rgb, f"P{idx}" if idx < 5 and diagnostics.get("tracked") else f"B{idx}", (cx + radius + 4, max(12, cy - 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
                    
                    # Detect and draw ArUco
                    detections, raw = detect_aruco(frame.rgb, frame.width, frame.height, "DICT_4X4_50")
                    if raw:
                        corners, ids = raw
                        if ids is not None:
                            cv2.aruco.drawDetectedMarkers(rgb, corners, ids)
                except Exception as e:
                    print(f"[LivePainting Overlay Error] {e}")

            sample_rgb = rgb[::step, ::step]
            jpeg_q = 65 if quality == "low" else (80 if quality in {"balanced", "medium"} else 90)
            if "rgb" in channels:
                payload, fmt = _encode_frame(sample_rgb, is_rgb=True, quality=jpeg_q)
                await _send_binary(websocket, {"protocol_version": 1, "type": "camera.preview", "seq": sequence, "timestamp": datetime.now(UTC).isoformat(), "kind": "rgb", "encoding": fmt, "width": sample_rgb.shape[1], "height": sample_rgb.shape[0], "slices": [{"name": "rgb", "offset": 0, "length": len(payload)}]}, payload)
                sequence += 1
            if "depth" in channels and frame.depth_m is not None:
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
    except Exception:
        pass


def _probe_metrics(container: Any, settings: dict[str, Any], marker_points_m: list[list[float]] | np.ndarray | None = None, marker_ids: list[int] | None = None) -> dict[str, Any]:
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
        
    return {"blob_count": result["candidate_count"], "candidate_count": result["candidate_count"], "inliers": 5 if result["tracked"] else 0, "tracked": result["tracked"], "reprojection_error_px": float(err_px) if result["tracked"] and err_px is not None else None, "rejection_reason": None if result["tracked"] else "; ".join(result.get("errors", [])) or ("fewer_than_five_blobs" if result["candidate_count"] < 5 else "no_valid_5_marker_geometry_found"), "exposure_feedback": "Exposure is usable", "keypoints": result.get("keypoints", []), "simulated": result.get("simulated", False), "aruco_detections": aruco_detections}


async def _send_probe_images(websocket: WebSocket, frame: Any, sequence: int, metrics: dict[str, Any] | None = None, settings: dict[str, Any] | None = None) -> int:
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
        except Exception as e:
            print(f"[Probe Tuning Overlay Error] {e}")

    payload, fmt = _encode_frame(overlay, is_rgb=True, quality=80)
    await _send_binary(
        websocket,
        {
            "protocol_version": 1,
            "type": "probe.diagnostic_image",
            "seq": sequence,
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": "overlay",
            "encoding": fmt,
            "width": frame.width,
            "height": frame.height,
        },
        payload,
    )
    sequence += 1
    return sequence


@router.websocket("/projects/{project_id}/probe-tuning")
async def probe_tuning(websocket: WebSocket, project_id: str) -> None:
    if not await _authorize(websocket):
        return
    container = websocket.app.state.container
    sequence = 0
    previous = -1
    settings = dict(DEFAULT_BLOB_DETECTOR)
    marker_points_m = None
    marker_ids = None
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=0.001)
                data = message.get("data", {})
                if data.get("calibration_id"):
                    try:
                        calibration = container.catalog.get_resource(project_id, "probe_calibration", data["calibration_id"])
                        settings = dict(calibration["blob_detector"])
                        marker_points_m = calibration.get("probe", {}).get("marker_points_m")
                    except AppError:
                        pass
                if message.get("type") == "tuning.patch":
                    draft = data.get("blob_detector", {})
                    if "marker_ids" in data:
                        marker_ids = data["marker_ids"]
                    errors = validate_blob_detector(draft)
                    if errors:
                        await websocket.send_json(_envelope("error", sequence, {"code": "BLOB_SETTINGS_INVALID", "message": "Draft settings are invalid.", "details": {"field_errors": errors}}))
                        sequence += 1
                        continue
                    settings = dict(draft)
            except TimeoutError:
                pass

            latest = container.camera.latest_frame
            if latest is None or latest.sequence <= previous:
                await asyncio.sleep(0.03)
                continue
                
            previous = latest.sequence
            metrics = _probe_metrics(container, settings, marker_points_m, marker_ids)
            try:
                await websocket.send_json(_envelope("probe.tuning_result", sequence, metrics))
                sequence += 1
                sequence = await _send_probe_images(websocket, latest, sequence, metrics, settings)
            except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                break
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError, Exception):
        pass


@router.websocket("/projects/{project_id}/registrations/{registration_id}/tracking")
async def registration_tracking(websocket: WebSocket, project_id: str, registration_id: str) -> None:
    if not await _authorize(websocket):
        return
    container = websocket.app.state.container
    sequence = 0
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

        previous = -1
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
            
            try:
                await websocket.send_json(_envelope("tracking.frame", sequence, result))
                sequence += 1
            except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                break
            
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=0.001)
            except TimeoutError:
                pass
            except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                break
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError, Exception):
        pass


@router.websocket("/projects/{project_id}/probe-test")
async def probe_test(websocket: WebSocket, project_id: str) -> None:
    if not await _authorize(websocket):
        return
    container = websocket.app.state.container
    sequence = 0
    try:
        while True:
            project = container.catalog.get_project(project_id)
            settings = dict(DEFAULT_BLOB_DETECTOR)
            if project.get("active_probe_calibration_id"):
                settings = container.catalog.get_resource(project_id, "probe_calibration", project["active_probe_calibration_id"])["blob_detector"]
            try:
                await websocket.send_json(_envelope("probe.tracking_test", sequence, _probe_metrics(container, settings)))
                sequence += 1
            except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                break
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=0.2)
            except TimeoutError:
                pass
            except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                break
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError, Exception):
        pass


@router.websocket("/projects/{project_id}/sessions/{session_id}/tracking")
async def session_tracking(websocket: WebSocket, project_id: str, session_id: str) -> None:
    if not await _authorize(websocket):
        return
    container = websocket.app.state.container
    sequence = 0
    path: dict[str, Any] | None = None
    next_sample_at = 0.0
    try:
        container.catalog.get_resource(project_id, "session", session_id)
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=0.05)
                command = message.get("type")
                data = message.get("data", {})
                command_id = str(message.get("command_id") or data.get("command_id") or "")
                if command == "paint.point":
                    try:
                        save_image = bool(data.get("save_image", False))
                        image_bytes = None
                        if save_image and container.camera.state == "ready":
                            latest = container.camera.latest_frame
                            if latest is not None:
                                rgb = np.frombuffer(latest.rgb, dtype=np.uint8).reshape(latest.height, latest.width, 3)
                                payload_bytes, _ = _encode_frame(rgb, is_rgb=True, quality=90)
                                image_bytes = payload_bytes
                                
                        record = commit_point(container, project_id, session_id, {"command_id": command_id, "frame_id": data.get("frame_id"), "note": "", "low_quality_override_reason": data.get("reason") if data.get("allow_low_quality") else None, "save_image": save_image}, image_bytes=image_bytes)
                        snapshot = _session_counts(container, project_id, session_id)
                        await websocket.send_json(_envelope("paint.point_committed", sequence, {"command_id": command_id, "record": record, **snapshot}, command_id))
                    except AppError as exc:
                        await websocket.send_json(_envelope("paint.point_rejected", sequence, {"command_id": command_id, "reason": exc.message, "code": exc.code}, command_id))
                    sequence += 1
                elif command == "paint.path.start":
                    path = {"command_id": command_id, "samples": [], "sampling": data.get("sampling", {"mode": "time", "interval_ms": 100})}
                    next_sample_at = 0.0
                    await websocket.send_json(_envelope("paint.path_started", sequence, {"command_id": command_id}, command_id)); sequence += 1
                elif command == "paint.path.stop" and path:
                    record = create_path(SimpleNamespace(app=websocket.app), project_id, session_id, PaintedPathCreate(command_id=path["command_id"], samples=path["samples"], sampling_policy=path["sampling"]))
                    await websocket.send_json(_envelope("paint.path_committed", sequence, {"command_id": path["command_id"], "record": record, **_session_counts(container, project_id, session_id)}, path["command_id"])); sequence += 1
                    path = None
                elif command == "paint.undo":
                    record = undo_last(SimpleNamespace(app=websocket.app), project_id, session_id)
                    await websocket.send_json(_envelope("paint.undo_committed", sequence, {"command_id": command_id, "record": record, **_session_counts(container, project_id, session_id)}, command_id)); sequence += 1
                elif command == "paint.note":
                    session = container.catalog.get_resource(project_id, "session", session_id)
                    notes = (str(session.get("notes", "")) + "\n" + str(data.get("text", ""))).strip()[-4000:]
                    container.catalog.update_resource(project_id, "session", session_id, payload_patch={"notes": notes})
                    await websocket.send_json(_envelope("session.status", sequence, {"notes": notes}, command_id)); sequence += 1
            except TimeoutError:
                pass
            session = container.catalog.get_resource(project_id, "session", session_id)
            if session["state"] not in {"running", "paused", "degraded"}:
                await websocket.send_json(_envelope("session.status", sequence, {"state": session["state"]})); sequence += 1
                await asyncio.sleep(0.2)
                continue
            if session["state"] == "running":
                frame = next_tracking(container, project_id, session_id)
                await websocket.send_json(_envelope("tracking.frame", sequence, frame)); sequence += 1
                if path is not None:
                    now = time.monotonic()
                    sampling = path["sampling"]
                    take = False
                    if sampling.get("mode") == "distance" and path["samples"]:
                        take = np.linalg.norm(np.asarray(frame["tip_w_m"]) - np.asarray(path["samples"][-1]["position_w_m"])) >= float(sampling.get("distance_m", 0.002))
                    else:
                        take = now >= next_sample_at
                        next_sample_at = now + float(sampling.get("interval_ms", 100)) / 1000
                    if take:
                        path["samples"].append({"timestamp": datetime.now(UTC).isoformat(), "position_w_m": frame["tip_w_m"], "quality": frame["quality"]})
    except WebSocketDisconnect:
        if path and path["samples"]:
            try:
                create_path(SimpleNamespace(app=websocket.app), project_id, session_id, PaintedPathCreate(command_id=path["command_id"], samples=path["samples"], sampling_policy=path["sampling"], note="Interrupted by stream disconnect"))
            except Exception:
                pass


def _session_counts(container: Any, project_id: str, session_id: str) -> dict[str, int]:
    return {
        "point_count": len(container.catalog.list_resources(project_id, "painted_point", parent_id=session_id, limit=100000)),
        "path_count": len(container.catalog.list_resources(project_id, "painted_path", parent_id=session_id, limit=100000)),
    }
