"""Stable v1 transport shapes consumed by the React application.

These handlers intentionally sit before the richer collection envelopes in the domain
router. They keep the browser contract small while the underlying services remain shared.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

from fastapi import APIRouter, Request

from spatial_probe_atlas.api.projects_mapping import capture_frames as capture_frames_command
from spatial_probe_atlas.api.projects_mapping import create_map as create_map_command
from spatial_probe_atlas.api.schemas import CaptureFramesRequest, MapCreate
from spatial_probe_atlas.domain.errors import AppError


router = APIRouter()


def _capture_summary(value: dict[str, Any]) -> dict[str, Any]:
    metrics = value.get("quality_metrics") or value.get("quality") or {}
    if isinstance(metrics, str):
        metrics = {}
    state = value.get("state", "accepted")
    result = {**value, "quality_metrics": metrics, "quality": "accepted" if state == "accepted" else "rejected"}
    result["blur_score"] = float(metrics.get("blur_score", 0.0))
    luma = float(metrics.get("mean_luma", 0.0))
    result["exposure_state"] = "underexposed" if luma < 20 else "overexposed" if luma > 235 else "good"
    return result


def _set_summary(container: Any, project_id: str, value: dict[str, Any]) -> dict[str, Any]:
    frames = container.catalog.list_resources(project_id, "capture_frame", parent_id=value["capture_set_id"], include_deleted=True, limit=1000)
    accepted = sum(bool(frame.get("included", True)) for frame in frames)
    excluded = len(frames) - accepted
    # A conservative bounded proxy until full pose coverage analysis is produced.
    coverage = min(1.0, accepted / 30.0)
    return {**value, "frame_count": len(frames), "accepted_frame_count": accepted, "excluded_frame_count": excluded, "coverage": coverage}


@router.get("/camera/devices")
def camera_devices_array(request: Request) -> list[dict[str, Any]]:
    return request.app.state.container.camera.devices()


@router.get("/projects/{project_id}/capture-sets")
def capture_sets_array(request: Request, project_id: str) -> list[dict[str, Any]]:
    container = request.app.state.container
    return [_set_summary(container, project_id, item) for item in container.catalog.list_resources(project_id, "capture_set")]


@router.get("/projects/{project_id}/capture-sets/{capture_set_id}")
def capture_set_browser_shape(request: Request, project_id: str, capture_set_id: str) -> dict[str, Any]:
    container = request.app.state.container
    value = _set_summary(container, project_id, container.catalog.get_resource(project_id, "capture_set", capture_set_id))
    value["frames"] = [_capture_summary(item) for item in container.catalog.list_resources(project_id, "capture_frame", parent_id=capture_set_id, include_deleted=True, limit=1000)]
    return value


@router.post("/projects/{project_id}/capture-sets/{capture_set_id}/frames:capture", status_code=201)
async def capture_one_browser_shape(request: Request, project_id: str, capture_set_id: str, body: CaptureFramesRequest | None = None) -> Any:
    command = body or CaptureFramesRequest()
    result = await capture_frames_command(request, project_id, capture_set_id, command)
    items = [_capture_summary(item) for item in result["items"]]
    return items[0] if len(items) == 1 else items


@router.get("/projects/{project_id}/maps")
def maps_array(request: Request, project_id: str) -> list[dict[str, Any]]:
    return request.app.state.container.catalog.list_resources(project_id, "scene_map")


@router.post("/projects/{project_id}/maps", status_code=202)
def create_map_browser_shape(request: Request, project_id: str, body: MapCreate) -> dict[str, Any]:
    if body.compute_profile == "cuda":
        raise AppError("CUDA_MAPPING_PROFILE_NOT_INSTALLED", "The v1 CUDA mapping integration is not installed in this source build.", status_code=503, suggested_action="Use Auto/CPU; the functional depth-assisted CPU pipeline remains available.")
    normalized = body.model_copy(update={"compute_profile": "cpu"})
    result = create_map_command(request, project_id, normalized)
    return {**result, "id": result["map_id"], "name": body.name, "point_count": 0, "map_id": result["map_id"]}


@router.post("/projects/{project_id}/reveal")
def reveal_project(request: Request, project_id: str) -> dict[str, Any]:
    container = request.app.state.container
    container.catalog.get_project(project_id)
    path = container.artifacts.project_dir(project_id)
    try:
        path.relative_to(container.settings.data_root.resolve())
    except ValueError as exc:
        raise AppError("PROJECT_PATH_INVALID", "The project path is outside the configured data root.", status_code=500) from exc
    if os.name != "nt":
        raise AppError("REVEAL_NOT_AVAILABLE", "Reveal in File Explorer is only available on Windows.", status_code=503)
    subprocess.Popen(["explorer.exe", str(path)], close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"revealed": True, "project_id": project_id}
