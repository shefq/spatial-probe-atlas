from __future__ import annotations

import math
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse

from spatial_probe_atlas.api.schemas import ExportCreate, PaintedPathCreate, PaintedPointCreate, RecordPatch, SessionCreate, SessionNote
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.pipelines.tracking.replay import make_replay_tracking_frame, quality_gate
from spatial_probe_atlas.services.review_export import (
    ensure_session_review_mutable,
    freeze_review_filters,
    query_review_records,
    verify_export_artifact,
)


router = APIRouter()


def _session_preflight(container: Any, project_id: str, session: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    project = container.catalog.get_project(project_id)
    free = shutil.disk_usage(container.settings.data_root).free
    checks = [
        {"key": "camera", "label": "Camera ready", "passed": container.camera.project_id == project_id and container.camera.state == "ready", "detail": container.camera.state, "required_route": f"/projects/{project_id}/camera"},
        {"key": "map", "label": "Metric map active", "passed": bool(project["active_map_id"]), "required_route": f"/projects/{project_id}/mapping"},
        {"key": "probe", "label": "Probe calibration active", "passed": bool(project["active_probe_calibration_id"]), "required_route": f"/projects/{project_id}/registration"},
        {"key": "registration", "label": "Metric registration active", "passed": bool(project["active_registration_id"]), "required_route": f"/projects/{project_id}/registration"},
        {"key": "storage", "label": "Storage reserve available", "passed": free > container.settings.disk_reserve_bytes, "detail": f"{free} bytes free"},
    ]
    if session:
        checks.extend([
            {"key": "map_revision", "label": "Session map revision unchanged", "passed": session.get("map_id") == project["active_map_id"]},
            {"key": "calibration_revision", "label": "Session probe revision unchanged", "passed": session.get("probe_calibration_id") == project["active_probe_calibration_id"]},
            {"key": "registration_revision", "label": "Session registration revision unchanged", "passed": session.get("registration_id") == project["active_registration_id"]},
        ])
    return checks


def _record_view(value: dict[str, Any]) -> dict[str, Any]:
    record_type = "point" if value["kind"] == "painted_point" else "path"
    result = {**value, "type": record_type, "session_id": value["parent_id"]}
    if record_type == "point":
        result["id"] = value["point_id"]
    else:
        result["id"] = value["path_id"]
        result.setdefault("positions_w_m", [sample["position_w_m"] for sample in result.get("samples", [])])
        result.setdefault("sample_count", len(result["positions_w_m"]))
    return result


def _session_view(container: Any, project_id: str, session_id: str) -> dict[str, Any]:
    session = container.catalog.get_resource(project_id, "session", session_id)
    points = container.catalog.list_resources(project_id, "painted_point", parent_id=session_id, limit=100000)
    paths = container.catalog.list_resources(project_id, "painted_path", parent_id=session_id, limit=100000)
    started = session.get("started_at")
    ended = session.get("ended_at")
    if started:
        start_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(str(ended).replace("Z", "+00:00")) if ended else datetime.now(UTC)
        duration = max(0.0, (end_dt - start_dt).total_seconds())
    else:
        duration = 0.0
    tracking = container.tracking_snapshots.get(session_id)
    recent = sorted([*_map_records(points), *_map_records(paths)], key=lambda item: item.get("timestamp", item.get("started_at", "")), reverse=True)[:20]
    return {
        **session, "point_count": sum(not item["deleted"] for item in points), "path_count": sum(not item["deleted"] for item in paths),
        "duration_seconds": duration, "size_bytes": int(session.get("size_bytes", 0)), "frame_count": int(session.get("frame_count", 0)),
        "tracked_ratio": float(session.get("tracked_ratio", 1.0 if tracking else 0.0)), "preflight": _session_preflight(container, project_id, session),
        "tracking": tracking, "recent_records": recent,
    }


def _map_records(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_record_view(item) for item in values]


@router.get("/projects/{project_id}/sessions")
def list_sessions(request: Request, project_id: str) -> list[dict[str, Any]]:
    container = request.app.state.container
    return [_session_view(container, project_id, item["session_id"]) for item in container.catalog.list_resources(project_id, "session", limit=1000)]


@router.post("/projects/{project_id}/sessions", status_code=201)
def create_session(request: Request, project_id: str, body: SessionCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    container = request.app.state.container
    cached = container.catalog.idempotent_response(f"session.create:{project_id}", idempotency_key)
    if cached:
        return cached
    project = container.catalog.get_project(project_id)
    scene_map = container.catalog.get_resource(project_id, "scene_map", project["active_map_id"]) if project["active_map_id"] else None
    probe_calibration = container.catalog.get_resource(project_id, "probe_calibration", project["active_probe_calibration_id"]) if project["active_probe_calibration_id"] else None
    registration = container.catalog.get_resource(project_id, "registration", project["active_registration_id"]) if project["active_registration_id"] else None
    payload = {
        "map_id": project["active_map_id"], "probe_calibration_id": project["active_probe_calibration_id"],
        "registration_id": project["active_registration_id"], "notes": body.notes, "compute_profile": "cpu_replay_tracking",
        "map_revision": scene_map["revision"] if scene_map else None,
        "probe_calibration_revision": probe_calibration["revision"] if probe_calibration else None,
        "registration_revision": registration["revision"] if registration else None,
        "started_at": None, "ended_at": None, "frame_count": 0, "point_count": 0, "path_count": 0, "size_bytes": 0,
        "sampling_policy": {"mode": "time", "interval_ms": 100},
    }
    created = container.catalog.create_resource(project_id, "session", state="draft", name=body.name, payload=payload)
    result = _session_view(container, project_id, created["session_id"])
    container.catalog.save_idempotent_response(f"session.create:{project_id}", idempotency_key, result)
    return result


@router.get("/projects/{project_id}/sessions/{session_id}")
def get_session(request: Request, project_id: str, session_id: str) -> dict[str, Any]:
    return _session_view(request.app.state.container, project_id, session_id)


def _lifecycle(container: Any, project_id: str, session_id: str, action: str) -> dict[str, Any]:
    session = container.catalog.get_resource(project_id, "session", session_id)
    transitions = {
        "start": ({"draft", "preflight", "recoverable"}, "running"), "pause": ({"running", "degraded"}, "paused"),
        "resume": ({"paused", "degraded"}, "running"), "stop": ({"running", "paused", "degraded"}, "stopped"),
        "finalize": ({"stopped", "recoverable"}, "finalized"),
    }
    allowed, target = transitions[action]
    if session["state"] not in allowed:
        if session["state"] == target:
            return _session_view(container, project_id, session_id)
        raise AppError("SESSION_STATE_CONFLICT", f"Cannot {action} a session in state {session['state']}.", status_code=409)
    patch: dict[str, Any] = {}
    if action in {"start", "resume"}:
        checks = _session_preflight(container, project_id, session)
        failures = [item for item in checks if not item["passed"]]
        if failures:
            container.catalog.update_resource(project_id, "session", session_id, state="preflight")
            raise AppError("SESSION_PREFLIGHT_FAILED", "Session preflight has unmet requirements.", status_code=409, details={"checks": checks})
    if action == "start":
        patch["started_at"] = datetime.now(UTC).isoformat()
        container.tracking_sequences[session_id] = 0
    if action in {"stop", "finalize"}:
        patch["ended_at"] = session.get("ended_at") or datetime.now(UTC).isoformat()
    container.catalog.update_resource(project_id, "session", session_id, state=target, payload_patch=patch)
    return _session_view(container, project_id, session_id)


for _action in ("start", "pause", "resume", "stop", "finalize"):
    def _make(action: str):
        def endpoint(request: Request, project_id: str, session_id: str) -> dict[str, Any]:
            return _lifecycle(request.app.state.container, project_id, session_id, action)
        endpoint.__name__ = f"session_{action}"
        return endpoint
    router.add_api_route(f"/projects/{{project_id}}/sessions/{{session_id}}/{_action}", _make(_action), methods=["POST"])


def next_tracking(container: Any, project_id: str, session_id: str) -> dict[str, Any]:
    session = container.catalog.get_resource(project_id, "session", session_id)
    sequence = int(container.tracking_sequences.get(session_id, 0))
    calibration = container.catalog.get_resource(project_id, "probe_calibration", session["probe_calibration_id"]) if session.get("probe_calibration_id") else None
    transform = calibration.get("probe", {}).get("t_marker_tip") if calibration else None
    frame = make_replay_tracking_frame(session_id, sequence, transform)
    container.tracking_sequences[session_id] = sequence + 1
    container.tracking_snapshots[session_id] = frame
    if sequence % 10 == 0:
        container.catalog.update_resource(project_id, "session", session_id, payload_patch={"frame_count": sequence + 1, "last_tracking_at": datetime.now(UTC).isoformat(), "tracked_ratio": 1.0})
    return frame


@router.get("/projects/{project_id}/sessions/{session_id}/tracking")
def tracking_snapshot(request: Request, project_id: str, session_id: str) -> dict[str, Any]:
    container = request.app.state.container
    session = container.catalog.get_resource(project_id, "session", session_id)
    if session["state"] in {"running", "paused", "degraded"} and session_id not in container.tracking_snapshots:
        next_tracking(container, project_id, session_id)
    return _session_view(container, project_id, session_id)


def commit_point(container: Any, project_id: str, session_id: str, body: PaintedPointCreate | dict[str, Any]) -> dict[str, Any]:
    data = body.model_dump() if isinstance(body, PaintedPointCreate) else body
    command_id = str(data.get("command_id") or data.get("commandId") or "")
    cached = container.catalog.idempotent_response(f"paint.point:{session_id}", command_id)
    if cached:
        return cached
    session = container.catalog.get_resource(project_id, "session", session_id)
    if session["state"] != "running":
        raise AppError("SESSION_NOT_RUNNING", "Points can only be painted while the session is running.", status_code=409)
    frame = container.tracking_snapshots.get(session_id) or next_tracking(container, project_id, session_id)
    accepted, reasons = quality_gate(frame)
    override = str(data.get("low_quality_override_reason") or "").strip()
    if not accepted and not override:
        raise AppError("PAINT_QUALITY_REJECTED", "The point did not pass tracking quality gates.", status_code=422, details={"reasons": reasons})
    position = data.get("position_w_m") or frame["tip_w_m"]
    if len(position) != 3 or not np.isfinite(position).all():
        raise AppError("PAINT_POSITION_INVALID", "Paint coordinates must be a finite map-frame XYZ triple.", status_code=422)
    timestamp = datetime.now(UTC).isoformat()
    payload = {
        "type": "point", "session_id": session_id, "command_id": command_id, "frame_id": data.get("frame_id") or frame["frame_id"],
        "timestamp": timestamp, "position_w_m": list(map(float, position)), "orientation_w_xyzw": [0, 0, 0, 1],
        "quality": "flagged_low_quality" if not accepted else str(data.get("quality") or frame["quality"]), "note": data.get("note", ""),
        "override_reason": override or None, "metrics": {"camera_inliers": frame["camera_inliers"], "camera_reprojection_error_px": frame["camera_reprojection_error_px"], "probe_inliers": frame["probe_inliers"], "probe_reprojection_error_px": frame["probe_reprojection_error_px"], "latency_ms": frame["latency_ms"]},
        "coordinate_frame": "W", "units": "m",
    }
    created = container.catalog.create_resource(project_id, "painted_point", state="flagged_low_quality" if not accepted else "committed", parent_id=session_id, payload=payload)
    result = _record_view(created)
    container.catalog.save_idempotent_response(f"paint.point:{session_id}", command_id, result)
    return result


@router.post("/projects/{project_id}/sessions/{session_id}/painted-points", status_code=201)
def create_point(request: Request, project_id: str, session_id: str, body: PaintedPointCreate) -> dict[str, Any]:
    return commit_point(request.app.state.container, project_id, session_id, body)


def _path_length(samples: list[dict[str, Any]]) -> float:
    if len(samples) < 2:
        return 0.0
    points = np.asarray([item["position_w_m"] for item in samples], dtype=float)
    return float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())


@router.post("/projects/{project_id}/sessions/{session_id}/painted-paths", status_code=201)
def create_path(request: Request, project_id: str, session_id: str, body: PaintedPathCreate) -> dict[str, Any]:
    container = request.app.state.container
    cached = container.catalog.idempotent_response(f"paint.path:{session_id}", body.command_id)
    if cached:
        return cached
    session = container.catalog.get_resource(project_id, "session", session_id)
    if session["state"] != "running":
        raise AppError("SESSION_NOT_RUNNING", "Paths can only be painted while the session is running.", status_code=409)
    samples = body.samples
    if not samples:
        samples = [{"timestamp": datetime.now(UTC).isoformat(), "position_w_m": (container.tracking_snapshots.get(session_id) or next_tracking(container, project_id, session_id))["tip_w_m"], "quality": "good"}]
    if len(samples) > 2000 or any(len(item.get("position_w_m", [])) != 3 or not np.isfinite(item["position_w_m"]).all() for item in samples):
        raise AppError("PATH_SAMPLES_INVALID", "Path chunks are limited to 2,000 finite map-frame samples.", status_code=422)
    # Deduplicate sub-millimetre jitter while retaining endpoints.
    deduplicated = [samples[0]]
    for item in samples[1:]:
        if np.linalg.norm(np.asarray(item["position_w_m"]) - np.asarray(deduplicated[-1]["position_w_m"])) >= 0.0005:
            deduplicated.append(item)
    now = datetime.now(UTC).isoformat()
    payload = {"type": "path", "session_id": session_id, "command_id": body.command_id, "started_at": deduplicated[0].get("timestamp", now), "ended_at": deduplicated[-1].get("timestamp", now), "timestamp": deduplicated[0].get("timestamp", now), "samples": deduplicated, "positions_w_m": [item["position_w_m"] for item in deduplicated], "sample_count": len(deduplicated), "length_m": _path_length(deduplicated), "sampling_policy": body.sampling_policy, "quality": "good", "note": body.note, "coordinate_frame": "W", "units": "m"}
    result = _record_view(container.catalog.create_resource(project_id, "painted_path", state="committed", parent_id=session_id, payload=payload))
    container.catalog.save_idempotent_response(f"paint.path:{session_id}", body.command_id, result)
    return result


def _review_page(
    container: Any,
    project_id: str,
    session_id: str,
    *,
    cursor: str | None,
    include_deleted: bool,
    limit: int,
    record_type: str,
    quality: str,
    start: str | None,
    end: str | None,
) -> dict[str, Any]:
    container.catalog.get_resource(project_id, "session", session_id)
    filters = freeze_review_filters(
        {"type": record_type, "quality": quality, "from": start, "to": end, "include_deleted": include_deleted}
    )
    return query_review_records(container.database, project_id, session_id, filters, cursor=cursor, limit=limit)


@router.get("/projects/{project_id}/sessions/{session_id}/painted-points")
def painted_points(
    request: Request, project_id: str, session_id: str, cursor: str | None = None,
    include_deleted: bool = False, quality: str = "all",
    start: str | None = Query(default=None, alias="from"), end: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    return _review_page(request.app.state.container, project_id, session_id, cursor=cursor, include_deleted=include_deleted, limit=limit, record_type="point", quality=quality, start=start, end=end)


@router.get("/projects/{project_id}/sessions/{session_id}/painted-paths")
def painted_paths(
    request: Request, project_id: str, session_id: str, cursor: str | None = None,
    include_deleted: bool = False, quality: str = "all",
    start: str | None = Query(default=None, alias="from"), end: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    return _review_page(request.app.state.container, project_id, session_id, cursor=cursor, include_deleted=include_deleted, limit=limit, record_type="path", quality=quality, start=start, end=end)


@router.get("/projects/{project_id}/sessions/{session_id}/painted-records")
def painted_records(
    request: Request, project_id: str, session_id: str, cursor: str | None = None,
    include_deleted: bool = False, record_type: str = Query(default="all", alias="type"), quality: str = "all",
    start: str | None = Query(default=None, alias="from"), end: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    return _review_page(request.app.state.container, project_id, session_id, cursor=cursor, include_deleted=include_deleted, limit=limit, record_type=record_type, quality=quality, start=start, end=end)


def _record_kind(record_type: str) -> str:
    if record_type not in {"point", "path"}:
        raise AppError("PAINT_RECORD_TYPE_INVALID", "Paint record type must be point or path.", status_code=422)
    return f"painted_{record_type}"


@router.patch("/projects/{project_id}/sessions/{session_id}/painted-{record_type}s/{record_id}")
def patch_record(request: Request, project_id: str, session_id: str, record_type: str, record_id: str, body: RecordPatch) -> dict[str, Any]:
    container = request.app.state.container
    ensure_session_review_mutable(container.catalog.get_resource(project_id, "session", session_id))
    kind = _record_kind(record_type)
    record = container.catalog.get_resource(project_id, kind, record_id)
    if record["parent_id"] != session_id:
        raise AppError("PAINT_RECORD_NOT_FOUND", "The record does not belong to this session.", status_code=404)
    return _record_view(container.catalog.update_resource(project_id, kind, record_id, payload_patch=body.model_dump(exclude_none=True)))


@router.delete("/projects/{project_id}/sessions/{session_id}/painted-{record_type}s/{record_id}", status_code=204)
def delete_record(request: Request, project_id: str, session_id: str, record_type: str, record_id: str) -> None:
    container = request.app.state.container
    ensure_session_review_mutable(container.catalog.get_resource(project_id, "session", session_id))
    kind = _record_kind(record_type)
    record = container.catalog.get_resource(project_id, kind, record_id)
    if record["parent_id"] != session_id:
        raise AppError("PAINT_RECORD_NOT_FOUND", "The record does not belong to this session.", status_code=404)
    container.catalog.delete_resource(project_id, kind, record_id)


@router.post("/projects/{project_id}/sessions/{session_id}/painted-{record_type}s/{record_id}/restore")
def restore_record(request: Request, project_id: str, session_id: str, record_type: str, record_id: str) -> dict[str, Any]:
    container = request.app.state.container
    ensure_session_review_mutable(container.catalog.get_resource(project_id, "session", session_id))
    kind = _record_kind(record_type)
    record = container.catalog.get_resource(project_id, kind, record_id)
    if record["parent_id"] != session_id:
        raise AppError("PAINT_RECORD_NOT_FOUND", "The record does not belong to this session.", status_code=404)
    return _record_view(container.catalog.update_resource(project_id, kind, record_id, deleted=False))


@router.post("/projects/{project_id}/sessions/{session_id}/undo")
def undo_last(request: Request, project_id: str, session_id: str) -> dict[str, Any]:
    container = request.app.state.container
    records = _map_records(container.catalog.list_resources(project_id, "painted_point", parent_id=session_id, limit=100000) + container.catalog.list_resources(project_id, "painted_path", parent_id=session_id, limit=100000))
    if not records:
        raise AppError("PAINT_UNDO_EMPTY", "There is no committed paint record to undo.", status_code=409)
    latest = max(records, key=lambda item: item.get("timestamp", item.get("started_at", "")))
    kind = _record_kind(latest["type"])
    return _record_view(container.catalog.update_resource(project_id, kind, latest["id"], deleted=True))


@router.get("/projects/{project_id}/sessions/{session_id}/replay")
def replay(request: Request, project_id: str, session_id: str, from_: float = 0, to: float = float("inf")) -> dict[str, Any]:
    records = _map_records(request.app.state.container.catalog.list_resources(project_id, "painted_point", parent_id=session_id, include_deleted=True, limit=100000) + request.app.state.container.catalog.list_resources(project_id, "painted_path", parent_id=session_id, include_deleted=True, limit=100000))
    records.sort(key=lambda item: item.get("timestamp", item.get("started_at", "")))
    return {"records": records[int(max(from_, 0)): int(to) if math.isfinite(to) else None], "coordinate_frame": "W", "units": "m"}


@router.get("/projects/{project_id}/sessions/{session_id}/exports")
def list_exports(request: Request, project_id: str, session_id: str) -> list[dict[str, Any]]:
    container = request.app.state.container
    container.catalog.get_resource(project_id, "session", session_id)
    values = container.catalog.list_resources(project_id, "export", parent_id=session_id, limit=1000)
    return [
        {
            **item,
            "id": item["export_id"],
            "session_id": session_id,
            "checksum_sha256": item.get("sha256") or item.get("checksum_sha256"),
            "download_url": f"/api/v1/projects/{project_id}/sessions/{session_id}/exports/{item['export_id']}/download" if item["state"] == "completed" else None,
        }
        for item in values
    ]


@router.post("/projects/{project_id}/sessions/{session_id}/exports", status_code=202)
def create_export(request: Request, project_id: str, session_id: str, body: ExportCreate) -> dict[str, Any]:
    container = request.app.state.container
    session = container.catalog.get_resource(project_id, "session", session_id)
    frozen = freeze_review_filters(body.filters, include_deleted=body.include_deleted)
    frozen_at = datetime.now(UTC).isoformat()
    export = container.catalog.create_resource(
        project_id, "export", state="queued", parent_id=session_id,
        payload={
            "session_id": session_id, "format": body.format, "filters": frozen,
            "include_deleted": frozen["include_deleted"], "size_bytes": 0, "checksum_sha256": None,
            "session_revision": session["revision"], "frozen_at": frozen_at,
        },
    )
    job = container.catalog.create_job(
        project_id=project_id, owner_id=export["export_id"], type="session_export",
        spec={
            "stage_count": 3, "session_id": session_id, "format": body.format, "filters": frozen,
            "include_deleted": frozen["include_deleted"], "frozen_at": frozen_at,
            "session_revision": session["revision"],
        },
    )
    updated = container.catalog.update_resource(project_id, "export", export["export_id"], payload_patch={"job_id": job["job_id"]})
    container.jobs.submit(job["job_id"])
    return {**updated, "id": updated["export_id"], "job_id": job["job_id"], "session_id": session_id}


@router.get("/projects/{project_id}/sessions/{session_id}/exports/{export_id}/download")
def download_export(request: Request, project_id: str, session_id: str, export_id: str) -> FileResponse:
    container = request.app.state.container
    export = container.catalog.get_resource(project_id, "export", export_id)
    if export["parent_id"] != session_id or export["state"] != "completed" or not export.get("relative_uri"):
        raise AppError("EXPORT_NOT_READY", "The requested export is not ready to download.", status_code=409)
    checksum = export.get("sha256") or export.get("checksum_sha256")
    if not checksum:
        raise AppError("EXPORT_METADATA_INVALID", "The export has no checksum metadata.", status_code=409)
    path = verify_export_artifact(container.artifacts, export["relative_uri"], checksum)
    return FileResponse(
        path,
        media_type=export.get("media_type"),
        filename=export.get("download_filename") or path.name,
        headers={
            "ETag": f'"{checksum}"',
            "X-Content-SHA256": checksum,
            "Cache-Control": "private, max-age=31536000, immutable",
        },
    )


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict[str, Any]:
    return request.app.state.container.catalog.get_job(job_id)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    return request.app.state.container.jobs.cancel(job_id)


@router.post("/jobs/{job_id}/resume")
def resume_job(request: Request, job_id: str) -> dict[str, Any]:
    return request.app.state.container.jobs.resume(job_id)
