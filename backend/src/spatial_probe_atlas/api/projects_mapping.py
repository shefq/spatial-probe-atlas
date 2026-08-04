from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import FileResponse

from spatial_probe_atlas.api.schemas import (
    CameraConnectRequest,
    CaptureFramesRequest,
    CaptureSetCreate,
    CloneRequest,
    FrameImportRequest,
    FrameUpdate,
    MapCreate,
    ProjectCreate,
    ProjectUpdate,
)
from spatial_probe_atlas.domain.errors import AppError


router = APIRouter()


def _revision_from_if_match(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.strip('W/"'))
    except ValueError as exc:
        raise AppError("ETAG_INVALID", "If-Match must contain a numeric resource revision.", status_code=400) from exc


def _with_etag(response: Response, result: dict[str, Any]) -> dict[str, Any]:
    response.headers["ETag"] = f'W/"{result.get("revision", 1)}"'
    return result


@router.get("/projects")
def list_projects(request: Request, include_archived: bool = False) -> dict[str, Any]:
    items = request.app.state.container.catalog.list_projects(include_archived=include_archived)
    return {"items": items, "count": len(items), "next_cursor": None}


@router.post("/projects", status_code=201)
def create_project(request: Request, body: ProjectCreate, response: Response, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    catalog = request.app.state.container.catalog
    cached = catalog.idempotent_response("project.create", idempotency_key)
    if cached:
        return _with_etag(response, cached)
    result = catalog.create_project(body.name)
    catalog.save_idempotent_response("project.create", idempotency_key, result)
    return _with_etag(response, result)


@router.get("/projects/{project_id}")
def get_project(request: Request, project_id: str, response: Response) -> dict[str, Any]:
    return _with_etag(response, request.app.state.container.catalog.get_project(project_id))


@router.patch("/projects/{project_id}")
def patch_project(request: Request, project_id: str, body: ProjectUpdate, response: Response, if_match: str | None = Header(default=None, alias="If-Match")) -> dict[str, Any]:
    result = request.app.state.container.catalog.update_project(project_id, body.model_dump(exclude_none=True), expected_revision=_revision_from_if_match(if_match))
    return _with_etag(response, result)


@router.post("/projects/{project_id}/clone", status_code=201)
def clone_project(request: Request, project_id: str, body: CloneRequest) -> dict[str, Any]:
    return request.app.state.container.catalog.clone_project(project_id, body.name)


@router.post("/projects/{project_id}/archive")
def archive_project(request: Request, project_id: str) -> dict[str, Any]:
    return request.app.state.container.catalog.set_project_state(project_id, "archived")


@router.post("/projects/{project_id}/restore")
def restore_project(request: Request, project_id: str) -> dict[str, Any]:
    return request.app.state.container.catalog.set_project_state(project_id, "active")


@router.get("/projects/{project_id}/summary")
def project_summary(request: Request, project_id: str) -> dict[str, Any]:
    return request.app.state.container.catalog.project_summary(project_id)


@router.get("/projects/{project_id}/jobs")
def project_jobs(request: Request, project_id: str) -> dict[str, Any]:
    items = request.app.state.container.catalog.list_jobs(project_id)
    return {"items": items, "count": len(items)}


@router.get("/camera/devices")
def camera_devices(request: Request) -> dict[str, Any]:
    devices = request.app.state.container.camera.devices()
    return {"items": devices, "count": len(devices), "primary_adapter": "record3d"}


@router.get("/camera/status")
def camera_status(request: Request) -> dict[str, Any]:
    return request.app.state.container.camera.status()


@router.post("/camera/connect")
async def camera_connect(request: Request, body: CameraConnectRequest) -> dict[str, Any]:
    request.app.state.container.catalog.get_project(body.project_id)
    return await request.app.state.container.camera.connect(project_id=body.project_id, adapter_name=body.adapter, device_id=body.device_id, owner=body.owner)


@router.post("/camera/disconnect", status_code=204)
async def camera_disconnect(request: Request) -> None:
    await request.app.state.container.camera.disconnect()


@router.get("/projects/{project_id}/capture-sets")
def capture_sets(request: Request, project_id: str) -> dict[str, Any]:
    items = request.app.state.container.catalog.list_resources(project_id, "capture_set")
    return {"items": items, "count": len(items), "next_cursor": None}


@router.post("/projects/{project_id}/capture-sets", status_code=201)
def create_capture_set(request: Request, project_id: str, body: CaptureSetCreate) -> dict[str, Any]:
    return request.app.state.container.catalog.create_resource(project_id, "capture_set", state="draft", name=body.name, payload={"source": body.source, "frame_count": 0, "accepted_frame_count": 0, "size_bytes": 0, "quality": {}, "frozen_revision": None})


@router.get("/projects/{project_id}/capture-sets/{capture_set_id}")
def get_capture_set(request: Request, project_id: str, capture_set_id: str) -> dict[str, Any]:
    result = request.app.state.container.catalog.get_resource(project_id, "capture_set", capture_set_id)
    frames = request.app.state.container.catalog.list_resources(project_id, "capture_frame", parent_id=capture_set_id, limit=1000)
    result["frames"] = frames
    return result


def _frame_quality(rgb: np.ndarray) -> dict[str, Any]:
    gray = rgb.mean(axis=2)
    variance = float(np.var(np.diff(gray, axis=0))) + float(np.var(np.diff(gray, axis=1)))
    exposure = float(gray.mean())
    return {"blur_score": variance, "mean_luma": exposure, "blur_warning": variance < 20, "exposure_warning": exposure < 20 or exposure > 235, "duplicate": False}


def _persist_frame(container: Any, project_id: str, capture_set_id: str, *, sequence: int, timestamp_ns: int, width: int, height: int, k: list[float], rgb_bytes: bytes, depth_values: np.ndarray, source: str) -> dict[str, Any]:
    frame_id = __import__("uuid").uuid4().hex
    base = Path("captures") / capture_set_id / "frames" / frame_id
    rgb_path = container.artifacts.project_path(project_id, base.with_suffix(".rgb8"))
    depth_path = container.artifacts.project_path(project_id, base.with_suffix(".depth.f32"))
    intrinsics_path = container.artifacts.project_path(project_id, base.with_suffix(".intrinsics.json"))
    rgb_artifact = container.artifacts.atomic_write_bytes(rgb_path, rgb_bytes)
    depth = np.asarray(depth_values, dtype="<f4").reshape(height, width)
    depth_artifact = container.artifacts.atomic_write_bytes(depth_path, depth.tobytes())
    intrinsic_artifact = container.artifacts.atomic_write_json(intrinsics_path, {"schema_version": "1.0.0", "source": "record3d_per_frame" if source in {"record3d", "replay"} else "import", "width": width, "height": height, "intrinsic_matrix": k, "frame_sequence": sequence, "timestamp_ns": timestamp_ns})
    quality = _frame_quality(np.frombuffer(rgb_bytes, dtype=np.uint8).reshape(height, width, 3))
    payload = {"sequence": sequence, "device_timestamp_ns": timestamp_ns, "width": width, "height": height, "intrinsic_matrix": k, "intrinsics_source": "record3d_per_frame" if source in {"record3d", "replay"} else "import", "rgb_artifact": rgb_artifact, "depth_artifact": depth_artifact, "intrinsics_artifact": intrinsic_artifact, "checksum": hashlib.sha256(rgb_bytes + depth.tobytes()).hexdigest(), "quality": quality, "included": True, "source": source}
    return container.catalog.create_resource(project_id, "capture_frame", state="accepted", parent_id=capture_set_id, payload=payload, resource_id=frame_id)


def _refresh_capture_set(container: Any, project_id: str, capture_set_id: str) -> dict[str, Any]:
    frames = container.catalog.list_resources(project_id, "capture_frame", parent_id=capture_set_id, limit=1000)
    accepted = [item for item in frames if item.get("included", True)]
    size = sum(int(item.get("rgb_artifact", {}).get("size_bytes", 0)) + int(item.get("depth_artifact", {}).get("size_bytes", 0)) for item in frames)
    warnings = []
    if len(accepted) < 30:
        warnings.append({"code": "FRAME_COUNT_LOW", "message": "30 or more accepted frames are recommended."})
    return container.catalog.update_resource(project_id, "capture_set", capture_set_id, payload_patch={"frame_count": len(frames), "accepted_frame_count": len(accepted), "size_bytes": size, "warnings": warnings})


@router.post("/projects/{project_id}/capture-sets/{capture_set_id}/frames:capture", status_code=201)
async def capture_frames(request: Request, project_id: str, capture_set_id: str, body: CaptureFramesRequest) -> dict[str, Any]:
    container = request.app.state.container
    capture_set = container.catalog.get_resource(project_id, "capture_set", capture_set_id)
    if capture_set["state"] != "draft":
        raise AppError("CAPTURE_SET_FROZEN", "Frames cannot be added after a capture set is frozen.", status_code=409)
    if container.camera.project_id != project_id or container.camera.state != "ready":
        raise AppError("CAMERA_NOT_READY", "Connect and verify a camera for this project before capture.", status_code=409)
    items: list[dict[str, Any]] = []
    previous = -1
    for index in range(body.count):
        frame = await container.camera.wait_for_frame(previous, timeout=2.0)
        previous = frame.sequence
        depth = (np.frombuffer(frame.depth_m, dtype=np.float32) if isinstance(frame.depth_m, bytes) else np.asarray(frame.depth_m, dtype=np.float32)) if frame.depth_m is not None else np.full(frame.width * frame.height, np.nan, dtype=np.float32)
        items.append(_persist_frame(container, project_id, capture_set_id, sequence=frame.sequence, timestamp_ns=frame.device_timestamp_ns, width=frame.width, height=frame.height, k=list(frame.intrinsic_matrix), rgb_bytes=frame.rgb, depth_values=depth, source=str(capture_set.get("source", "record3d"))))
        if body.interval_ms and index + 1 < body.count:
            await asyncio.sleep(body.interval_ms / 1000)
    summary = _refresh_capture_set(container, project_id, capture_set_id)
    return {"items": items, "count": len(items), "capture_set": summary}


@router.post("/projects/{project_id}/capture-sets/{capture_set_id}/frames:import", status_code=201)
def import_frames(request: Request, project_id: str, capture_set_id: str, body: FrameImportRequest) -> dict[str, Any]:
    container = request.app.state.container
    capture_set = container.catalog.get_resource(project_id, "capture_set", capture_set_id)
    if capture_set["state"] != "draft":
        raise AppError("CAPTURE_SET_FROZEN", "Frames cannot be imported after a capture set is frozen.", status_code=409)
    items = []
    for index, input_frame in enumerate(body.frames):
        rgb = base64.b64decode(input_frame.rgb_base64, validate=True)
        depth_raw = base64.b64decode(input_frame.depth_f32_base64, validate=True)
        expected_rgb, expected_depth = input_frame.width * input_frame.height * 3, input_frame.width * input_frame.height * 4
        if len(rgb) != expected_rgb or len(depth_raw) != expected_depth:
            raise AppError("FRAME_PAYLOAD_SIZE_INVALID", "Imported RGB/depth bytes do not match declared dimensions.", status_code=422)
        depth = np.frombuffer(depth_raw, dtype="<f4")
        items.append(_persist_frame(container, project_id, capture_set_id, sequence=index, timestamp_ns=input_frame.timestamp_ns or index, width=input_frame.width, height=input_frame.height, k=input_frame.intrinsic_matrix, rgb_bytes=rgb, depth_values=depth, source="import"))
    return {"items": items, "count": len(items), "capture_set": _refresh_capture_set(container, project_id, capture_set_id)}


@router.patch("/projects/{project_id}/capture-sets/{capture_set_id}/frames/{frame_id}")
def patch_frame(request: Request, project_id: str, capture_set_id: str, frame_id: str, body: FrameUpdate) -> dict[str, Any]:
    frame = request.app.state.container.catalog.get_resource(project_id, "capture_frame", frame_id)
    if frame["parent_id"] != capture_set_id:
        raise AppError("FRAME_NOT_IN_CAPTURE_SET", "The frame does not belong to this capture set.", status_code=404)
    result = request.app.state.container.catalog.update_resource(project_id, "capture_frame", frame_id, payload_patch=body.model_dump())
    _refresh_capture_set(request.app.state.container, project_id, capture_set_id)
    return result


@router.get("/projects/{project_id}/maps")
def maps(request: Request, project_id: str) -> dict[str, Any]:
    items = request.app.state.container.catalog.list_resources(project_id, "scene_map")
    return {"items": items, "count": len(items), "next_cursor": None}


@router.post("/projects/{project_id}/maps", status_code=202)
def create_map(request: Request, project_id: str, body: MapCreate) -> dict[str, Any]:
    container = request.app.state.container
    capture_set = container.catalog.get_resource(project_id, "capture_set", body.capture_set_id)
    if body.capture_set_revision is not None and capture_set["revision"] != body.capture_set_revision:
        raise AppError("CAPTURE_SET_REVISION_STALE", "The selected capture set has changed.", status_code=412)
    frames = [item for item in container.catalog.list_resources(project_id, "capture_frame", parent_id=body.capture_set_id, limit=1000) if item.get("included", True)]
    if len(frames) < container.settings.min_mapping_frames:
        raise AppError("MAPPING_FRAMES_INSUFFICIENT", f"At least {container.settings.min_mapping_frames} accepted frames are required.", status_code=422, details={"accepted_frames": len(frames)})
    free = shutil.disk_usage(container.settings.data_root).free
    estimated_peak = max(capture_set.get("size_bytes", 0) * 4, 512 * 1024**2)
    if estimated_peak + container.settings.disk_reserve_bytes > free:
        raise AppError("INSUFFICIENT_STORAGE", "Estimated mapping peak plus reserve exceeds free disk space.", status_code=507, details={"estimated_peak_bytes": estimated_peak, "reserve_bytes": container.settings.disk_reserve_bytes, "free_bytes": free})
    cuda_ready = False
    if importlib.util.find_spec("torch"):
        try:
            import torch
            cuda_ready = bool(torch.cuda.is_available())
        except Exception:
            pass
    if body.compute_profile == "cuda" and not cuda_ready:
        raise AppError("CUDA_NOT_AVAILABLE", "CUDA was requested explicitly but did not pass capability checks.", status_code=503, suggested_action="Choose CPU or Auto.")
    effective = "cuda_aliked_lightglue" if body.compute_profile in {"auto", "cuda"} and cuda_ready else "cpu_depth_assisted_replay"
    scene_map = container.catalog.create_resource(project_id, "scene_map", state="building", name=body.name, parent_id=body.capture_set_id, payload={"capture_set_id": body.capture_set_id, "capture_set_revision": capture_set["revision"], "requested_compute_profile": body.compute_profile, "effective_compute_profile": effective, "point_count": 0, "units": "unscaled"})
    frame_ids = [frame["frame_id"] for frame in frames]
    job = container.catalog.create_job(project_id=project_id, owner_id=scene_map["map_id"], type="mapping", spec={"stage_count": 10, "frame_ids": frame_ids, "capture_set_id": body.capture_set_id, "effective_compute_profile": effective})
    container.catalog.update_resource(project_id, "capture_set", body.capture_set_id, state="processing", payload_patch={"frozen_revision": capture_set["revision"]})
    container.jobs.submit(job["job_id"])
    return {"map_id": scene_map["map_id"], "job_id": job["job_id"], "state": "queued", "effective_compute_profile": effective}


@router.get("/projects/{project_id}/maps/{map_id}")
def get_map(request: Request, project_id: str, map_id: str) -> dict[str, Any]:
    return request.app.state.container.catalog.get_resource(project_id, "scene_map", map_id)


@router.post("/projects/{project_id}/maps/{map_id}/activate")
def activate_map(request: Request, project_id: str, map_id: str) -> dict[str, Any]:
    scene_map = request.app.state.container.catalog.get_resource(project_id, "scene_map", map_id)
    if scene_map["state"] not in {"ready_unscaled", "ready_metric", "active"}:
        raise AppError("MAP_NOT_READY", "Only a validated published map can be activated.", status_code=409)
    return request.app.state.container.catalog.activate(project_id, "scene_map", map_id)


@router.get("/projects/{project_id}/maps/{map_id}/point-cloud/manifest")
def map_manifest(request: Request, project_id: str, map_id: str, response: Response) -> Any:
    scene_map = request.app.state.container.catalog.get_resource(project_id, "scene_map", map_id)
    manifest = scene_map.get("manifest")
    if not manifest:
        raise AppError("MAP_ARTIFACT_NOT_READY", "The map manifest has not been published.", status_code=409)
    path = request.app.state.container.artifacts.root / manifest["relative_uri"]
    if not path.is_file() or request.app.state.container.artifacts.sha256(path) != manifest["sha256"]:
        raise AppError("MAP_ARTIFACT_CORRUPT", "The map manifest is missing or failed its checksum.", status_code=500)
    response.headers["ETag"] = f'"{manifest["sha256"]}"'
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/projects/{project_id}/maps/{map_id}/point-cloud/tiles/{tile_id}")
def map_tile(request: Request, project_id: str, map_id: str, tile_id: str) -> FileResponse:
    if not tile_id.isalnum() or len(tile_id) > 64:
        raise AppError("TILE_ID_INVALID", "Tile identifiers must be alphanumeric.", status_code=400)
    scene_map = request.app.state.container.catalog.get_resource(project_id, "scene_map", map_id)
    manifest_info = scene_map.get("manifest")
    if not manifest_info:
        raise AppError("MAP_ARTIFACT_NOT_READY", "The map tiles have not been published.", status_code=409)
    manifest_path = request.app.state.container.artifacts.root / manifest_info["relative_uri"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    descriptor = manifest["tiles"].get(tile_id)
    if not descriptor:
        raise AppError("TILE_NOT_FOUND", "The requested point-cloud tile does not exist.", status_code=404)
    path = request.app.state.container.artifacts.root / descriptor["uri"]
    return FileResponse(path, media_type="application/octet-stream", headers={"ETag": f'"{descriptor["sha256"]}"', "Cache-Control": "public, max-age=31536000, immutable", "Accept-Ranges": "bytes"})
