from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import numpy as np
from fastapi import APIRouter, Header, Request

from spatial_probe_atlas.api import calibration_registration as calibration
from spatial_probe_atlas.api import hardware_contract, sessions_review
from spatial_probe_atlas.api.integrity_contract import _strict_session_preflight
from spatial_probe_atlas.api.schemas import SessionCreate
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.pipelines.tracking.indexed import IndexedCpuTrackingPipeline


router = APIRouter()


@router.post("/projects/{project_id}/probe-calibrations", status_code=201)
def probe_calibration_with_active_flag(request: Request, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
    result = hardware_contract.create_probe_calibration_hardware_safe(request, project_id, body)
    active_id = request.app.state.container.catalog.get_project(project_id).get("active_probe_calibration_id")
    return {**result, "active": result.get("probe_calibration_id") == active_id}


@router.post("/projects/{project_id}/probe-calibrations/validate")
async def validate_probe_transport(request: Request, project_id: str) -> dict[str, Any]:
    result = await calibration.validate_probe(request, project_id)
    warnings = [str(item.get("message") or item.get("code") or item) if isinstance(item, dict) else str(item) for item in result.get("warnings", [])]
    errors = [
        {"path": str(item.get("path") or "calibration"), "message": str(item.get("message") or item)} if isinstance(item, dict)
        else {"path": "calibration", "message": str(item)}
        for item in result.get("errors", [])
    ]
    return {**result, "warnings": warnings, "errors": errors}


@router.post("/projects/{project_id}/sessions", status_code=201)
def create_session_with_frozen_dependencies(
    request: Request,
    project_id: str,
    body: SessionCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    container = request.app.state.container
    cached = container.catalog.idempotent_response(f"session.create:{project_id}", idempotency_key)
    if cached:
        return cached
    checks = _strict_session_preflight(container, project_id)
    dependency_keys = {"map", "probe", "registration", "dependency_binding"}
    if any(not item["passed"] for item in checks if item["key"] in dependency_keys):
        raise AppError("SESSION_DEPENDENCY_PREFLIGHT_FAILED", "Create a session only after an exact active metric map, probe calibration, and registration are bound.", status_code=409, details={"checks": checks})
    project = container.catalog.get_project(project_id)
    scene_map = container.catalog.get_resource(project_id, "scene_map", project["active_map_id"])
    probe = container.catalog.get_resource(project_id, "probe_calibration", project["active_probe_calibration_id"])
    registration = container.catalog.get_resource(project_id, "registration", project["active_registration_id"])
    payload = {
        "map_id": scene_map["map_id"], "map_revision": scene_map["revision"],
        "probe_calibration_id": probe["probe_calibration_id"], "probe_calibration_revision": probe["revision"],
        "registration_id": registration["registration_id"], "registration_revision": registration["revision"],
        "metric_binding": scene_map.get("metric_binding"), "notes": body.notes,
        "requested_compute_profile": body.compute_profile,
        "effective_compute_profile": "replay_tracking_v1" if getattr(container.camera.adapter, "adapter_name", None) == "replay" else "cpu_sift_pnp_v1",
        "started_at": None, "ended_at": None, "frame_count": 0, "point_count": 0, "path_count": 0, "size_bytes": 0,
        "sampling_policy": {"mode": "time", "interval_ms": 100}, "active_path": None,
    }
    created = container.catalog.create_resource(project_id, "session", state="draft", name=body.name, payload=payload)
    result = sessions_review._session_view(container, project_id, created["session_id"])
    container.catalog.save_idempotent_response(f"session.create:{project_id}", idempotency_key, result)
    return result


def _registration_view(container: Any, project_id: str, registration_id: str) -> dict[str, Any]:
    value = container.catalog.get_resource(project_id, "registration", registration_id)
    active_id = container.catalog.get_project(project_id)["active_registration_id"]
    return {
        **value,
        "active": value["id"] == active_id,
        "validation_state": value.get("validation_status", "pending"),
        "rms_residual_mm": float(value.get("rms_residual_m", 0)) * 1000 if value.get("rms_residual_m") is not None else None,
        "max_residual_mm": float(value.get("max_residual_m", 0)) * 1000 if value.get("max_residual_m") is not None else None,
    }


@router.post("/projects/{project_id}/registrations/{registration_id}/observations", status_code=201)
def registration_observation_from_exact_map(request: Request, project_id: str, registration_id: str, body: dict[str, Any]) -> dict[str, Any]:
    container = request.app.state.container
    adapter = getattr(container.camera.adapter, "adapter_name", None)
    if ("camera_pose_w" in body and "probe_pose_c" in body) or ("source_point_m0" in body and "target_point_w" in body) or adapter in {None, "replay"}:
        calibration.add_registration_observation(request, project_id, registration_id, body)
        return _registration_view(container, project_id, registration_id)
    frame = container.camera.latest_frame
    if frame is None or container.camera.state != "ready":
        raise AppError("CAMERA_NOT_READY", "A current Record3D frame is required for board observation.", status_code=409)
    registration = container.catalog.get_resource(project_id, "registration", registration_id)
    probe = container.catalog.get_resource(project_id, "probe_calibration", registration["probe_calibration_id"])
    scene_map = container.catalog.get_resource(project_id, "scene_map", registration["map_id"])
    from spatial_probe_atlas.pipelines.tracking.factory import create_tracking_pipeline
    localizer = create_tracking_pipeline(scene_map, {"scale": 1.0, "rotation": np.eye(3).reshape(-1).tolist(), "translation": [0, 0, 0]}, probe, container.artifacts.root)
    t_m0_c, inliers, error, reason = localizer._localize(frame)
    if t_m0_c is None or inliers < 30 or error > 3.0:
        raise AppError("MAP_LOCALIZATION_REJECTED", "The current frame could not be localized to the exact published reference map.", status_code=422, retryable=True, details={"inliers": inliers, "reprojection_error_px": error if math.isfinite(error) else None, "reason": reason})
    t_c_b, metrics = hardware_contract._detect_board(frame)
    t_m0_b = t_m0_c @ t_c_b
    board_points = np.asarray([[-0.035, -0.0225, 0], [0.035, -0.0225, 0], [0.035, 0.0225, 0], [-0.035, 0.0225, 0]], dtype=float)
    for index, point_b in enumerate(board_points):
        source = (t_m0_b @ np.r_[point_b, 1])[:3].tolist()
        container.catalog.create_resource(project_id, "registration_observation", state="accepted", parent_id=registration_id, payload={"source_point_m0": source, "target_point_w": point_b.tolist(), "label": f"board_corner_{index}", "source": "record3d_aruco_current_frame", "captured_at": datetime.now(UTC).isoformat(), "board_metrics": metrics, "camera_localization": {"inliers": inliers, "reprojection_error_px": error}, "t_c_b": t_c_b.reshape(-1).tolist()})
    observations = container.catalog.list_resources(project_id, "registration_observation", parent_id=registration_id, limit=1000)
    container.catalog.update_resource(project_id, "registration", registration_id, payload_patch={"observation_count": len(observations), "last_board_metrics": metrics})
    return _registration_view(container, project_id, registration_id)
