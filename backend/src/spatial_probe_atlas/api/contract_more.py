from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request

from spatial_probe_atlas.api import calibration_registration as calibration
from spatial_probe_atlas.api import projects_mapping, sessions_review, system
from spatial_probe_atlas.api.schemas import MapCreate, RegistrationCreate, RegistrationValidationRequest, SessionCreate
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.pipelines.tracking.runtime import set_runtime_container


router = APIRouter()


def _active(value: dict[str, Any], active_id: str | None) -> dict[str, Any]:
    return {**value, "active": value["id"] == active_id}


def _registration(value: dict[str, Any], active_id: str | None) -> dict[str, Any]:
    return {
        **_active(value, active_id),
        "validation_state": value.get("validation_status", "pending"),
        "rms_residual_mm": float(value.get("rms_residual_m", 0.0)) * 1000 if value.get("rms_residual_m") is not None else None,
        "max_residual_mm": float(value.get("max_residual_m", 0.0)) * 1000 if value.get("max_residual_m") is not None else None,
        "t_w_b": value.get("t_w_b") or [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    }


@router.get("/system/capabilities")
def capabilities_contract(request: Request) -> dict[str, Any]:
    result = system._capabilities(request)
    compute = result["compute"]
    return {
        **result, "app_version": result["application_version"], "compute_state": compute["state"],
        "effective_compute_profile": compute["effective"], "gpu": compute.get("device"),
        "record3d_state": result["record3d"]["state"], "replay_available": True,
        "data_root": "<local-data-root>",
    }


@router.get("/system/resources")
def resources_contract(request: Request) -> dict[str, Any]:
    value = system._resources(request)
    memory, disk, process = value.get("memory", {}), value["disk"], value.get("process", {})
    warnings = [{**item, "id": item["code"].lower(), "message": item["code"].replace("_", " ").title(), "suggested_action": "Close other applications or free local disk space."} for item in value["warnings"]]
    return {
        **value, "cpu_percent": process.get("cpu_percent"), "ram_used_percent": memory.get("percent"),
        "ram_total_bytes": memory.get("total_bytes"), "disk_free_bytes": disk["free_bytes"], "disk_total_bytes": disk["total_bytes"],
        "vram_used_percent": None, "vram_total_bytes": None, "warnings": warnings,
    }


@router.get("/camera/status")
def camera_status_contract(request: Request) -> dict[str, Any]:
    value = request.app.state.container.camera.status()
    resolution = value.get("resolution") or {}
    return {**value, "frames_received": value.get("frame_count", 0), "rgb_width": resolution.get("width"), "rgb_height": resolution.get("height"), "depth_width": resolution.get("width"), "depth_height": resolution.get("height"), "depth_aligned": value.get("depth_alignment") == "rgb_aligned", "complete_frame_streak": min(int(value.get("frame_count", 0)), 5)}


@router.get("/projects/{project_id}/summary")
def project_summary_contract(request: Request, project_id: str) -> dict[str, Any]:
    container = request.app.state.container
    summary = container.catalog.project_summary(project_id)
    sessions = sessions_review.list_sessions(request, project_id)
    readiness = summary["readiness"]
    return {
        **summary, "capture_frame_count": summary["frame_count"],
        "map_point_count": sum(int(item.get("point_count", 0)) for item in container.catalog.list_resources(project_id, "scene_map")),
        "sessions": sessions, "jobs": summary.pop("active_jobs", []),
        "readiness": {
            "camera_ready": container.camera.project_id == project_id and container.camera.state == "ready",
            "map_ready": readiness["map"], "probe_calibration_ready": readiness["probe_calibration"],
            "registration_ready": readiness["registration"], "storage_ready": True,
        },
    }


@router.post("/projects/{project_id}/clone", status_code=201)
def clone_contract(request: Request, project_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    project = request.app.state.container.catalog.clone_project(project_id, (body or {}).get("name"))
    return {"project": project}


@router.post("/projects/{project_id}/capture-sets", status_code=201)
def create_capture_set_contract(request: Request, project_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = body or {}
    return request.app.state.container.catalog.create_resource(project_id, "capture_set", state="draft", name=str(data.get("name") or "Capture set"), payload={"source": data.get("source", "record3d"), "frame_count": 0, "accepted_frame_count": 0, "excluded_frame_count": 0, "coverage": 0.0, "size_bytes": 0, "quality": {}, "frozen_revision": None})


@router.post("/projects/{project_id}/maps", status_code=202)
def create_map_contract(request: Request, project_id: str, body: MapCreate) -> dict[str, Any]:
    if body.compute_profile == "cuda":
        raise AppError("CUDA_MAPPING_PROFILE_NOT_INSTALLED", "CUDA mapping assets are not installed in this source build.", status_code=503, suggested_action="Use Auto or CPU.")
    result = projects_mapping.create_map(request, project_id, body.model_copy(update={"compute_profile": "cpu"}))
    request.app.state.container.catalog.update_resource(project_id, "scene_map", result["map_id"], payload_patch={"job_id": result["job_id"]})
    return {"id": result["map_id"], "map_id": result["map_id"], "project_id": project_id, "name": body.name, "state": result["state"], "active": False, "capture_set_id": body.capture_set_id, "capture_set_revision": body.capture_set_revision, "point_count": 0, "job_id": result["job_id"], "effective_compute_profile": "cpu_depth_assisted_replay", "units": "arbitrary", "created_at": request.app.state.container.catalog.get_resource(project_id, "scene_map", result["map_id"])["created_at"]}


@router.get("/projects/{project_id}/maps/{map_id}/point-cloud/manifest")
def map_manifest_contract(request: Request, project_id: str, map_id: str) -> dict[str, Any]:
    from . import projects_mapping
    from fastapi import Response
    return projects_mapping.map_manifest(request, project_id, map_id, Response())


@router.get("/projects/{project_id}/probe-calibrations")
def probe_list_contract(request: Request, project_id: str) -> list[dict[str, Any]]:
    container = request.app.state.container
    active_id = container.catalog.get_project(project_id)["active_probe_calibration_id"]
    return [_active(item, active_id) for item in container.catalog.list_resources(project_id, "probe_calibration")]


@router.post("/projects/{project_id}/probe-captures/{capture_id}/frames:capture", status_code=201)
async def probe_capture_frame_contract(request: Request, project_id: str, capture_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    await calibration.capture_probe_frames(request, project_id, capture_id, body)
    value = request.app.state.container.catalog.get_resource(project_id, "probe_capture", capture_id)
    return {**value, "input_frame_count": value.get("frame_count", 0)}


@router.get("/projects/{project_id}/registrations")
def registration_list_contract(request: Request, project_id: str) -> list[dict[str, Any]]:
    container = request.app.state.container
    active_id = container.catalog.get_project(project_id)["active_registration_id"]
    return [_registration(item, active_id) for item in container.catalog.list_resources(project_id, "registration")]


@router.post("/projects/{project_id}/registrations", status_code=201)
def registration_create_contract(request: Request, project_id: str, body: RegistrationCreate) -> dict[str, Any]:
    value = calibration.create_registration(request, project_id, body)
    return _registration(value, request.app.state.container.catalog.get_project(project_id)["active_registration_id"])


@router.post("/projects/{project_id}/registrations/{registration_id}/observations", status_code=201)
def registration_observation_contract(request: Request, project_id: str, registration_id: str, body: dict[str, Any]) -> dict[str, Any]:
    calibration.add_registration_observation(request, project_id, registration_id, body)
    value = request.app.state.container.catalog.get_resource(project_id, "registration", registration_id)
    return _registration(value, request.app.state.container.catalog.get_project(project_id)["active_registration_id"])


@router.post("/projects/{project_id}/registrations/{registration_id}/solve")
def registration_solve_contract(request: Request, project_id: str, registration_id: str) -> dict[str, Any]:
    value = calibration.solve_registration(request, project_id, registration_id)
    return _registration(value, request.app.state.container.catalog.get_project(project_id)["active_registration_id"])


@router.post("/projects/{project_id}/registrations/{registration_id}/validate")
def registration_validate_contract(request: Request, project_id: str, registration_id: str, body: RegistrationValidationRequest) -> dict[str, Any]:
    value = calibration.validate_registration(request, project_id, registration_id, body)
    return _registration(value, request.app.state.container.catalog.get_project(project_id)["active_registration_id"])


@router.post("/projects/{project_id}/registrations/{registration_id}/activate")
def registration_activate_contract(request: Request, project_id: str, registration_id: str) -> dict[str, Any]:
    value = calibration.activate_registration(request, project_id, registration_id)
    return _registration(value, registration_id)


@router.post("/projects/{project_id}/sessions", status_code=201)
def session_create_contract(request: Request, project_id: str, body: SessionCreate) -> dict[str, Any]:
    set_runtime_container(request.app.state.container)
    return sessions_review.create_session(request, project_id, body, request.headers.get("Idempotency-Key"))


def _session_action(request: Request, project_id: str, session_id: str, action: str) -> dict[str, Any]:
    set_runtime_container(request.app.state.container)
    if action == "resume":
        session = request.app.state.container.catalog.get_resource(project_id, "session", session_id)
        if session["state"] == "recoverable":
            request.app.state.container.catalog.update_resource(project_id, "session", session_id, state="paused")
    return sessions_review._lifecycle(request.app.state.container, project_id, session_id, action)


for _action in ("start", "pause", "resume", "stop", "finalize"):
    def _make(action: str):
        def endpoint(request: Request, project_id: str, session_id: str) -> dict[str, Any]:
            return _session_action(request, project_id, session_id, action)
        endpoint.__name__ = f"contract_session_{action}"
        return endpoint
    router.add_api_route(f"/projects/{{project_id}}/sessions/{{session_id}}/{_action}", _make(_action), methods=["POST"])


@router.get("/projects/{project_id}/sessions/{session_id}/replay")
def replay_contract(request: Request, project_id: str, session_id: str, start: float = Query(default=0, alias="from"), end: float | None = Query(default=None, alias="to")) -> dict[str, Any]:
    records = sessions_review.painted_records(request, project_id, session_id, include_deleted=True, record_type="all", quality="all", limit=1000)["items"]
    # Numeric replay bounds are timeline indices; ISO timestamps are handled by painted-record filters.
    return {"records": records[int(max(start, 0)): int(end) if end is not None else None], "coordinate_frame": "W", "units": "m"}
