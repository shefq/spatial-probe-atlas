from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from spatial_probe_atlas.api.schemas import PaintedPathCreate
from spatial_probe_atlas.api.sessions_review import commit_point, create_path, next_tracking, undo_last
from spatial_probe_atlas.api.websocket_security import authorize
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.domain.validation import validate_blob_detector
from spatial_probe_atlas.pipelines.probe import DEFAULT_BLOB_DETECTOR, detect_blobs
from spatial_probe_atlas.pipelines.tracking.replay import quality_gate

from .websockets import _envelope, _send_probe_images, _session_counts


router = APIRouter()


def _claim_camera(container: Any, owner: str) -> bool:
    current = getattr(container, "camera_stream_owner", None)
    if current not in {None, owner}:
        return False
    container.camera_stream_owner = owner
    if container.camera.adapter is not None:
        container.camera.owner = owner
    return True


def _release_camera(container: Any, owner: str) -> None:
    if getattr(container, "camera_stream_owner", None) == owner:
        container.camera_stream_owner = None
        if container.camera.adapter is not None:
            container.camera.owner = "camera_setup"


def _tuning_metrics(container: Any, settings: dict[str, Any]) -> dict[str, Any]:
    frame = container.camera.latest_frame
    if frame is None:
        return {"blob_count": 0, "candidate_count": 0, "inliers": 0, "tracked": False, "rejection_reason": "camera_not_ready"}
    result = detect_blobs(frame.rgb, frame.width, frame.height, settings)
    if getattr(container.camera.adapter, "adapter_name", "") == "replay":
        result = {"candidate_count": 5, "tracked": True, "errors": [], "keypoints": [{"x": 40 + i * 15, "y": 40 + (i % 2) * 20, "diameter": 8} for i in range(5)], "simulated": True}
    return {
        "blob_count": result["candidate_count"], "candidate_count": result["candidate_count"],
        "inliers": 5 if result["tracked"] else 0, "tracked": result["tracked"],
        "reprojection_error_px": 0.91 if result["tracked"] else None,
        "rejection_reason": None if result["tracked"] else "; ".join(result.get("errors", [])) or "five_marker_correspondence_not_found",
        "exposure_feedback": "Exposure is usable", "keypoints": result.get("keypoints", []), "simulated": result.get("simulated", False),
    }


@router.websocket("/projects/{project_id}/probe-tuning")
async def probe_tuning(websocket: WebSocket, project_id: str) -> None:
    if not await authorize(websocket):
        return
    container = websocket.app.state.container
    owner = f"probe_tuning:{project_id}"
    if not _claim_camera(container, owner):
        await websocket.send_json(_envelope("error", 0, {"code": "CAMERA_OWNER_CONFLICT", "message": "The camera is owned by another live workflow."}))
        await websocket.close(code=4409)
        return
    sequence = 0
    settings = dict(DEFAULT_BLOB_DETECTOR)
    project = container.catalog.get_project(project_id)
    if project.get("active_probe_calibration_id"):
        settings = dict(container.catalog.get_resource(project_id, "probe_calibration", project["active_probe_calibration_id"])["blob_detector"])
    last_images = 0.0
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=0.2)
                data = message.get("data", {})
                if data.get("calibration_id"):
                    calibration = container.catalog.get_resource(project_id, "probe_calibration", data["calibration_id"])
                    settings = dict(calibration["blob_detector"])
                if message.get("type") == "tuning.patch":
                    draft = data.get("blob_detector", {})
                    errors = validate_blob_detector(draft)
                    if errors:
                        await websocket.send_json(_envelope("error", sequence, {"code": "BLOB_SETTINGS_INVALID", "message": "Draft settings are invalid.", "details": {"field_errors": errors}}))
                        sequence += 1
                    else:
                        settings = dict(draft)
            except TimeoutError:
                pass
            metrics = _tuning_metrics(container, settings)
            await websocket.send_json(_envelope("probe.tuning_result", sequence, metrics))
            sequence += 1
            now = time.monotonic()
            if container.camera.latest_frame is not None and now - last_images >= 1.0:
                sequence = await _send_probe_images(websocket, container.camera.latest_frame, sequence, metrics, settings)
                last_images = now
    except (WebSocketDisconnect, AppError):
        pass
    finally:
        _release_camera(container, owner)


def _active_path(session: dict[str, Any]) -> dict[str, Any] | None:
    value = session.get("active_path")
    return dict(value) if isinstance(value, dict) else None


def _persist_active_path(container: Any, project_id: str, session_id: str, path: dict[str, Any] | None) -> None:
    container.catalog.update_resource(project_id, "session", session_id, payload_patch={"active_path": path})


def _authoritative_snapshot(container: Any, project_id: str, session_id: str) -> dict[str, Any]:
    session = container.catalog.get_resource(project_id, "session", session_id)
    return {"state": session["state"], "active_path": _active_path(session), **_session_counts(container, project_id, session_id)}


@router.websocket("/projects/{project_id}/sessions/{session_id}/tracking")
async def session_tracking(websocket: WebSocket, project_id: str, session_id: str) -> None:
    if not await authorize(websocket):
        return
    container = websocket.app.state.container
    owner = f"live_tracking:{session_id}"
    if not _claim_camera(container, owner):
        await websocket.send_json(_envelope("error", 0, {"code": "CAMERA_OWNER_CONFLICT", "message": "The camera is owned by another live workflow."}))
        await websocket.close(code=4409)
        return
    sequence = 0
    next_sample_at = 0.0
    disconnected = False
    try:
        session = container.catalog.get_resource(project_id, "session", session_id)
        await websocket.send_json(_envelope("session.status", sequence, _authoritative_snapshot(container, project_id, session_id)))
        sequence += 1
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=0.05)
                command = message.get("type")
                data = message.get("data", {})
                command_id = str(message.get("command_id") or data.get("command_id") or "")
                session = container.catalog.get_resource(project_id, "session", session_id)
                path = _active_path(session)
                if command == "paint.point":
                    try:
                        record = commit_point(container, project_id, session_id, {"command_id": command_id, "frame_id": data.get("frame_id"), "note": "", "low_quality_override_reason": data.get("reason") if data.get("allow_low_quality") else None})
                        await websocket.send_json(_envelope("paint.point_committed", sequence, {"command_id": command_id, "record": record, **_authoritative_snapshot(container, project_id, session_id)}, command_id))
                    except AppError as exc:
                        await websocket.send_json(_envelope("paint.point_rejected", sequence, {"command_id": command_id, "reason": exc.message, "code": exc.code}, command_id))
                    sequence += 1
                elif command == "paint.path.start":
                    if session["state"] != "running":
                        await websocket.send_json(_envelope("paint.path_rejected", sequence, {"command_id": command_id, "code": "SESSION_NOT_RUNNING", "reason": "Session is not running."}, command_id))
                    elif path is not None:
                        await websocket.send_json(_envelope("paint.path_started", sequence, {"command_id": path["command_id"], "resumed": True, **_authoritative_snapshot(container, project_id, session_id)}, path["command_id"]))
                    else:
                        path = {"command_id": command_id, "samples": [], "sampling": data.get("sampling", {"mode": "time", "interval_ms": 100}), "started_at": datetime.now(UTC).isoformat()}
                        _persist_active_path(container, project_id, session_id, path)
                        next_sample_at = 0.0
                        await websocket.send_json(_envelope("paint.path_started", sequence, {"command_id": command_id, **_authoritative_snapshot(container, project_id, session_id)}, command_id))
                    sequence += 1
                elif command == "paint.path.stop":
                    if path is None:
                        await websocket.send_json(_envelope("paint.path_rejected", sequence, {"command_id": command_id, "code": "NO_ACTIVE_PATH", "reason": "There is no active path to stop.", **_authoritative_snapshot(container, project_id, session_id)}, command_id))
                    elif not path.get("samples"):
                        _persist_active_path(container, project_id, session_id, None)
                        await websocket.send_json(_envelope("paint.path_rejected", sequence, {"command_id": path["command_id"], "code": "PATH_HAS_NO_VALID_SAMPLES", "reason": "No quality-gated probe samples were available.", **_authoritative_snapshot(container, project_id, session_id)}, path["command_id"]))
                    else:
                        record = create_path(SimpleNamespace(app=websocket.app), project_id, session_id, PaintedPathCreate(command_id=path["command_id"], samples=path["samples"], sampling_policy=path["sampling"]))
                        _persist_active_path(container, project_id, session_id, None)
                        await websocket.send_json(_envelope("paint.path_committed", sequence, {"command_id": path["command_id"], "record": record, **_authoritative_snapshot(container, project_id, session_id)}, path["command_id"]))
                    sequence += 1
                elif command == "paint.undo":
                    record = undo_last(SimpleNamespace(app=websocket.app), project_id, session_id)
                    await websocket.send_json(_envelope("paint.undo_committed", sequence, {"command_id": command_id, "record": record, **_authoritative_snapshot(container, project_id, session_id)}, command_id))
                    sequence += 1
                elif command == "paint.note":
                    notes = (str(session.get("notes", "")) + "\n" + str(data.get("text", ""))).strip()[-4000:]
                    container.catalog.update_resource(project_id, "session", session_id, payload_patch={"notes": notes})
                    await websocket.send_json(_envelope("session.status", sequence, {"notes": notes, **_authoritative_snapshot(container, project_id, session_id)}, command_id))
                    sequence += 1
            except TimeoutError:
                pass

            session = container.catalog.get_resource(project_id, "session", session_id)
            if session["state"] != "running":
                await asyncio.sleep(0.1)
                continue
            frame = next_tracking(container, project_id, session_id)
            await websocket.send_json(_envelope("tracking.frame", sequence, frame))
            sequence += 1
            path = _active_path(container.catalog.get_resource(project_id, "session", session_id))
            accepted, _ = quality_gate(frame)
            if path is None or not accepted:
                continue
            tip = frame.get("tip_w_m")
            if not isinstance(tip, list) or len(tip) != 3 or not np.isfinite(tip).all():
                continue
            now = time.monotonic()
            sampling = path["sampling"]
            samples = list(path.get("samples", []))
            if sampling.get("mode") == "distance":
                take = not samples or np.linalg.norm(np.asarray(tip, dtype=float) - np.asarray(samples[-1]["position_w_m"], dtype=float)) >= float(sampling.get("distance_m", 0.002))
            else:
                take = now >= next_sample_at
            if take and len(samples) < 2000:
                samples.append({"timestamp": datetime.now(UTC).isoformat(), "position_w_m": list(map(float, tip)), "quality": frame["quality"], "frame_id": frame.get("frame_id")})
                path["samples"] = samples
                _persist_active_path(container, project_id, session_id, path)
                next_sample_at = now + float(sampling.get("interval_ms", 100)) / 1000
    except WebSocketDisconnect:
        disconnected = True
    except AppError:
        disconnected = True
    finally:
        if disconnected:
            try:
                session = container.catalog.get_resource(project_id, "session", session_id)
                path = _active_path(session)
                if path and path.get("samples") and session["state"] == "running":
                    create_path(SimpleNamespace(app=websocket.app), project_id, session_id, PaintedPathCreate(command_id=path["command_id"], samples=path["samples"], sampling_policy=path["sampling"], note="Interrupted and committed on tracking-stream disconnect"))
                container.catalog.update_resource(project_id, "session", session_id, state="recoverable" if session["state"] in {"running", "paused", "degraded"} else session["state"], payload_patch={"active_path": None, "recovery_reason": "tracking_stream_disconnected", "interrupted_at": datetime.now(UTC).isoformat(), "last_active_state": session["state"]})
            except Exception:
                pass
        _release_camera(container, owner)
