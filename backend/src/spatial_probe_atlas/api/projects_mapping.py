from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from spatial_probe_atlas.api.schemas import (
    CameraConnectRequest,
    CaptureFramesRequest,
    CaptureSetCreate,
    CloneRequest,
    FrameImportRequest,
    FrameUpdate,
    MapCreate,
    MapTransformUpdate,
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


def _enrich_frame(project_id: str, capture_set_id: str, frame: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(frame, dict):
        return frame
    item = dict(frame)
    frame_id = item.get("frame_id") or item.get("id")
    if frame_id:
        item["thumbnail_url"] = f"/api/v1/projects/{project_id}/capture-sets/{capture_set_id}/frames/{frame_id}/thumbnail"
    return item


@router.get("/projects/{project_id}/capture-sets/{capture_set_id}")
def get_capture_set(request: Request, project_id: str, capture_set_id: str) -> dict[str, Any]:
    result = request.app.state.container.catalog.get_resource(project_id, "capture_set", capture_set_id)
    frames = request.app.state.container.catalog.list_resources(project_id, "capture_frame", parent_id=capture_set_id, limit=1000)
    result["frames"] = [_enrich_frame(project_id, capture_set_id, f) for f in frames]
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
    if len(accepted) < 15:
        warnings.append({"code": "FRAME_COUNT_LOW", "message": "15 or more accepted frames are recommended."})
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
        if frame.depth_m is None:
            depth = np.full(frame.width * frame.height, np.nan, dtype=np.float32)
        elif isinstance(frame.depth_m, bytes):
            depth = np.frombuffer(frame.depth_m, dtype=np.float32)
        else:
            depth = np.asarray(frame.depth_m, dtype=np.float32)
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
    return _enrich_frame(project_id, capture_set_id, result)


@router.delete("/projects/{project_id}/capture-sets/{capture_set_id}/frames/{frame_id}", status_code=204)
def delete_frame(request: Request, project_id: str, capture_set_id: str, frame_id: str) -> None:
    frame = request.app.state.container.catalog.get_resource(project_id, "capture_frame", frame_id)
    if frame["parent_id"] != capture_set_id:
        raise AppError("FRAME_NOT_IN_CAPTURE_SET", "The frame does not belong to this capture set.", status_code=404)
    request.app.state.container.catalog.delete_resource(project_id, "capture_frame", frame_id)
    _refresh_capture_set(request.app.state.container, project_id, capture_set_id)


@router.get("/projects/{project_id}/capture-sets/{capture_set_id}/frames/{frame_id}/thumbnail")
def get_frame_thumbnail(request: Request, project_id: str, capture_set_id: str, frame_id: str) -> Response:
    container = request.app.state.container
    frame = container.catalog.get_resource(project_id, "capture_frame", frame_id)
    if frame.get("parent_id") != capture_set_id:
        raise AppError("FRAME_NOT_IN_CAPTURE_SET", "The frame does not belong to this capture set.", status_code=404)
    rgb_artifact = frame.get("rgb_artifact")
    if not rgb_artifact or "relative_uri" not in rgb_artifact:
        raise AppError("FRAME_IMAGE_NOT_FOUND", "Frame image metadata missing.", status_code=404)
    rgb_path = container.artifacts.root / rgb_artifact["relative_uri"]
    if not rgb_path.is_file():
        raise AppError("FRAME_IMAGE_NOT_FOUND", "Frame image file missing on disk.", status_code=404)
    width = int(frame.get("width", 640))
    height = int(frame.get("height", 480))
    rgb_bytes = rgb_path.read_bytes()
    try:
        from PIL import Image
        import io
        img = Image.frombytes("RGB", (width, height), rgb_bytes)
        img.thumbnail((240, 180))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return Response(content=buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400, immutable"})
    except Exception as exc:
        raise AppError("FRAME_IMAGE_CORRUPT", f"Failed to encode frame thumbnail: {exc}", status_code=500)


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


@router.post("/projects/{project_id}/maps/{map_id}/transform")
def save_map_transform(request: Request, project_id: str, map_id: str, body: MapTransformUpdate) -> dict[str, Any]:
    container = request.app.state.container
    scene_map = container.catalog.get_resource(project_id, "scene_map", map_id)
    if not scene_map:
        raise AppError("MAP_NOT_FOUND", "The requested map does not exist.", status_code=404)
    payload_patch = {"user_transform": body.model_dump()}
    container.catalog.update_resource(project_id, "scene_map", map_id, payload_patch=payload_patch)
    return {"status": "ok", "user_transform": body.model_dump()}


@router.post("/projects/{project_id}/maps/{map_id}/activate")
def activate_map(request: Request, project_id: str, map_id: str) -> dict[str, Any]:
    scene_map = request.app.state.container.catalog.get_resource(project_id, "scene_map", map_id)
    if scene_map["state"] not in {"ready_unscaled", "ready_metric", "active"}:
        raise AppError("MAP_NOT_READY", "Only a validated published map can be activated.", status_code=409)
    return request.app.state.container.catalog.activate(project_id, "scene_map", map_id)


class MeshGenerationRequest(BaseModel):
    openmvs_bin: str | None = None

@router.post("/projects/{project_id}/maps/{map_id}/mesh", status_code=202)
def generate_mesh(request: Request, project_id: str, map_id: str, body: MeshGenerationRequest) -> dict[str, Any]:
    container = request.app.state.container
    scene_map = container.catalog.get_resource(project_id, "scene_map", map_id)
    
    if scene_map["state"] not in {"ready_unscaled", "ready_metric", "active"}:
        raise AppError("MAP_NOT_READY", "Mesh generation requires a completed map.", status_code=409)

    # Validate that we have COLMAP export available in the map artifact
    map_dir = container.artifacts.project_path(project_id, Path("maps") / map_id)
    if not (map_dir / "colmap" / "0").exists():
        raise AppError("COLMAP_DATA_MISSING", "This map lacks COLMAP data needed for mesh generation. Please recreate the map.", status_code=400)

    job = container.catalog.create_job(
        project_id=project_id, 
        owner_id=map_id, 
        type="mesh", 
        spec={"stage_count": 4, "openmvs_bin": body.openmvs_bin}
    )
    container.jobs.submit(job["job_id"])
    return {"map_id": map_id, "job_id": job["job_id"], "state": "queued"}



def _extract_colmap_cameras(container: Any, project_id: str, map_dir: Path, capture_set_id: str | None = None) -> list[dict[str, Any]]:
    colmap_path = None
    for candidate in [map_dir / "sfm" / "models" / "0", map_dir / "sfm", map_dir]:
        if (candidate / "images.bin").is_file() or (candidate / "images.txt").is_file():
            colmap_path = candidate
            break
    if not colmap_path:
        return []
    raw_frames: list[dict[str, Any]] = []
    if capture_set_id:
        try:
            raw_frames = [item for item in container.catalog.list_resources(project_id, "capture_frame", parent_id=capture_set_id, limit=2000) if item.get("included", True)]
        except Exception:
            raw_frames = []
    try:
        import pycolmap
        rec = pycolmap.Reconstruction(str(colmap_path))
        cams = []
        for img_id, img in sorted(rec.images.items()):
            C = img.projection_center() if callable(img.projection_center) else img.projection_center
            cfw = img.cam_from_world() if callable(img.cam_from_world) else img.cam_from_world
            rot = cfw.rotation() if callable(cfw.rotation) else cfw.rotation
            qx, qy, qz, qw = rot.quat
            frame_id = None
            try:
                idx = int(Path(img.name).stem.split("-")[-1])
                if 0 <= idx < len(raw_frames):
                    frame_id = raw_frames[idx].get("id") or raw_frames[idx].get("frame_id")
            except (ValueError, IndexError):
                pass
            cams.append({
                "id": str(img_id),
                "name": img.name,
                "frame_id": frame_id or "",
                "position": [float(C[0]), float(C[1]), float(C[2])],
                "quaternion": [float(-qx), float(-qy), float(-qz), float(qw)],
            })
        return cams
    except Exception:
        return []


def _extract_colmap_markers(container: Any, project_id: str, map_dir: Path, capture_set_id: str | None = None, marker_size_m: float = 0.035) -> list[dict[str, Any]]:
    colmap_path = None
    for candidate in [map_dir / "sfm" / "models" / "0", map_dir / "sfm", map_dir]:
        if (candidate / "images.bin").is_file() or (candidate / "images.txt").is_file():
            colmap_path = candidate
            break
    if not colmap_path:
        return []
    raw_frames: list[dict[str, Any]] = []
    if capture_set_id:
        try:
            raw_frames = [item for item in container.catalog.list_resources(project_id, "capture_frame", parent_id=capture_set_id, limit=2000) if item.get("state") == "accepted" or item.get("included", True)]
        except Exception:
            raw_frames = []
    if not raw_frames:
        try:
            raw_frames = [item for item in container.catalog.list_resources(project_id, "capture_frame", limit=2000) if item.get("state") == "accepted" or item.get("included", True)]
        except Exception:
            raw_frames = []
    if not raw_frames:
        return []
    try:
        from spatial_probe_atlas.pipelines.mapping.align import extract_scene_markers
        return extract_scene_markers(
            artifact_root=container.artifacts.root,
            sfm_dir=colmap_path,
            frames_metadata=raw_frames,
            nominal_marker_size_m=marker_size_m,
        )
    except Exception:
        return []


def _aruco_marker_ids(value: Any) -> list[int]:
    try:
        marker_ids = list(dict.fromkeys(int(marker_id) for marker_id in value))
    except (TypeError, ValueError) as exc:
        raise AppError("ARUCO_MARKER_IDS_INVALID", "Marker IDs must be a non-empty list of integer IDs.", status_code=422) from exc
    if not marker_ids:
        raise AppError("ARUCO_MARKER_IDS_INVALID", "At least one ArUco marker ID is required.", status_code=422)
    return marker_ids


def _virtual_board_definition(body: dict[str, Any]) -> dict[str, Any]:
    marker_ids = _aruco_marker_ids(body.get("marker_ids", [6, 7, 5]))
    try:
        marker_size_m = float(body.get("nominal_marker_size_m", 0.035))
        separation_m = float(body.get("marker_separation_m", 0.0))
        columns = int(body.get("columns", len(marker_ids) if marker_ids else 1))
    except (TypeError, ValueError) as exc:
        raise AppError("ARUCO_BOARD_DIMENSIONS_INVALID", "Marker size, separation, and columns must be numeric values.", status_code=422) from exc
    if not np.isfinite(marker_size_m) or not 0.001 <= marker_size_m <= 1.0:
        raise AppError("ARUCO_MARKER_SIZE_INVALID", "The marker side length must be between 1 mm and 1 m.", status_code=422)
    if not np.isfinite(separation_m) or not 0.0 <= separation_m <= 1.0:
        raise AppError("ARUCO_MARKER_SEPARATION_INVALID", "The marker separation must be between 0 and 1 m.", status_code=422)
    if columns < 1:
        columns = 1
    if columns > len(marker_ids):
        columns = len(marker_ids)

    rows = int(np.ceil(len(marker_ids) / columns))
    pitch = marker_size_m + separation_m
    centres = []
    for index in range(len(marker_ids)):
        row, column = divmod(index, columns)
        centres.append([column * pitch, -row * pitch, 0.0])
    centres_array = np.asarray(centres, dtype=np.float64)
    centres_array -= centres_array.mean(axis=0, keepdims=True)
    half = marker_size_m / 2.0
    layout: dict[str, list[list[float]]] = {}
    for marker_id, (cx, cy, _) in zip(marker_ids, centres_array):
        layout[str(marker_id)] = [
            [float(cx - half), float(cy + half), 0.0],
            [float(cx + half), float(cy + half), 0.0],
            [float(cx + half), float(cy - half), 0.0],
            [float(cx - half), float(cy - half), 0.0],
        ]
    return {
        "dictionary": "DICT_4X4_50",
        "convention_version": 1,
        "marker_ids": marker_ids,
        "anchor_id": marker_ids[len(marker_ids) // 2],
        "marker_size_m": marker_size_m,
        "marker_separation_m": separation_m,
        "columns": columns,
        "rows": rows,
        "layout": layout,
    }


def _persist_aruco_board_calibration(container: Any, project_id: str, map_id: str, board: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    board_calibration_id = str(uuid.uuid4())
    document = {
        "schema_version": "1.0.0",
        "calibration_type": "aruco_board",
        "board_calibration_id": board_calibration_id,
        "name": str(name or "Virtual ArUco board"),
        "created_at": datetime.now(UTC).isoformat(),
        "units": "m",
        "board": board,
        "provenance": {"method": "virtual_aruco_board_definition_v1", "source": "scene_capture_mapping"},
    }
    artifact = container.artifacts.atomic_write_json(
        container.artifacts.project_path(project_id, Path("calibrations/aruco-board") / f"{board_calibration_id}.json"),
        document,
    )
    checksum = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    calibration = container.catalog.create_resource(
        project_id,
        "aruco_board_calibration",
        state="valid",
        name=document["name"],
        parent_id=map_id,
        resource_id=board_calibration_id,
        payload={**document, "artifact": artifact, "checksum": checksum},
    )
    container.catalog.update_resource(project_id, "scene_map", map_id, payload_patch={"aruco_board_calibration_id": board_calibration_id})
    return calibration


@router.post("/projects/{project_id}/maps/{map_id}/aruco-board-calibration", status_code=201)
def create_virtual_aruco_board_calibration(request: Request, project_id: str, map_id: str, body: dict[str, Any]) -> dict[str, Any]:
    container = request.app.state.container
    container.catalog.get_resource(project_id, "scene_map", map_id)
    board = _virtual_board_definition(body)
    calibration = _persist_aruco_board_calibration(container, project_id, map_id, board, body.get("name"))
    return {"board_calibration": calibration}


@router.get("/projects/{project_id}/aruco-board-calibrations/{board_calibration_id}/download")
def download_aruco_board_calibration(request: Request, project_id: str, board_calibration_id: str) -> FileResponse:
    calibration = request.app.state.container.catalog.get_resource(project_id, "aruco_board_calibration", board_calibration_id)
    artifact = calibration.get("artifact") or {}
    path = request.app.state.container.artifacts.root / str(artifact.get("relative_uri", ""))
    if not path.is_file():
        raise AppError("ARTIFACT_NOT_FOUND", "The ArUco board calibration artifact is missing.", status_code=404)
    return FileResponse(path, media_type="application/json", filename="aruco_board_calibration.json", headers={"ETag": f'"{artifact.get("sha256", "")}"'})


@router.post("/projects/{project_id}/maps/{map_id}/align-aruco")
def align_map_to_aruco_endpoint(request: Request, project_id: str, map_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from spatial_probe_atlas.pipelines.mapping.align import align_map_to_aruco
    container = request.app.state.container
    scene_map = container.catalog.get_resource(project_id, "scene_map", map_id)
    sfm = scene_map.get("sfm")
    if not isinstance(sfm, dict) or not sfm.get("relative_uri"):
        raise AppError(
            "SFM_ALIGNMENT_UNAVAILABLE",
            "ArUco alignment requires a completed CUDA SfM map with registered camera poses.",
            status_code=409,
            suggested_action="Build this capture using CUDA · ALIKED + LightGlue, then retry alignment.",
        )
    
    capture_id = body.get("probe_capture_id")
    if capture_id:
        frames = container.catalog.list_resources(project_id, "probe_capture_frame", parent_id=capture_id, limit=1000)
    else:
        capture_id = scene_map.get("parent_id")
        frames = container.catalog.list_resources(project_id, "capture_frame", parent_id=capture_id, limit=1000)
        
    accepted = [f for f in frames if f.get("state") == "accepted" or f.get("included", True)]
    if not accepted:
        raise AppError("CAPTURE_EMPTY", "No accepted frames in the capture.", status_code=422)
        
    sfm_dir = container.catalog.artifacts.root / sfm["relative_uri"]
    
    board_calibration_id = str(body.get("board_calibration_id") or scene_map.get("aruco_board_calibration_id") or "")
    if board_calibration_id:
        board_calibration = container.catalog.get_resource(project_id, "aruco_board_calibration", board_calibration_id)
        board_def = board_calibration.get("board")
        if not isinstance(board_def, dict):
            raise AppError("ARUCO_BOARD_CALIBRATION_INVALID", "The selected ArUco board calibration has no valid board definition.", status_code=422)
    else:
        # Preserve compatibility for the registration page: a board attached to
        # the active probe calibration is copied into a standalone immutable
        # board-calibration artifact before it is used for map alignment.
        active_calibration_id = container.catalog.get_project(project_id).get("active_probe_calibration_id")
        active_calibration = container.catalog.get_resource(project_id, "probe_calibration", active_calibration_id) if active_calibration_id else None
        source_board = active_calibration.get("board") if isinstance(active_calibration, dict) else None
        board_def = source_board if isinstance(source_board, dict) else _virtual_board_definition(body)
        board_calibration = _persist_aruco_board_calibration(container, project_id, map_id, board_def, body.get("board_name"))
        board_calibration_id = board_calibration["board_calibration_id"]

    marker_ids = _aruco_marker_ids(body.get("marker_ids", board_def.get("marker_ids", [])))
    board_dict = board_def.get("layout")
    if not isinstance(board_dict, dict):
        raise AppError("ARUCO_BOARD_LAYOUT_INVALID", "The selected ArUco board calibration has no marker-corner layout.", status_code=422)
    try:
        board_layout = {int(k): np.asarray(v, dtype=np.float32).reshape(4, 3) for k, v in board_dict.items()}
    except (TypeError, ValueError) as exc:
        raise AppError("ARUCO_BOARD_LAYOUT_INVALID", "The active virtual board has invalid marker-corner coordinates.", status_code=422) from exc
    missing_marker_ids = [marker_id for marker_id in marker_ids if marker_id not in board_layout]
    if missing_marker_ids:
        raise AppError("ARUCO_BOARD_LAYOUT_INCOMPLETE", "The selected marker IDs are absent from the active virtual-board layout.", status_code=422, details={"missing_marker_ids": missing_marker_ids})
    
    solution = align_map_to_aruco(
        artifact_root=container.catalog.artifacts.root,
        sfm_dir=sfm_dir,
        frames_metadata=accepted,
        marker_ids=marker_ids,
        board_layout=board_layout
    )
    try:
        max_rms_reprojection_error_px = float(body.get("max_rms_reprojection_error_px", 2.0))
    except (TypeError, ValueError) as exc:
        raise AppError("ALIGNMENT_THRESHOLD_INVALID", "The maximum RMS reprojection error must be a positive number of pixels.", status_code=422) from exc
    if not np.isfinite(max_rms_reprojection_error_px) or not 0.1 <= max_rms_reprojection_error_px <= 20.0:
        raise AppError("ALIGNMENT_THRESHOLD_INVALID", "The maximum RMS reprojection error must be between 0.1 and 20 px.", status_code=422)
    if solution["rms_reprojection_error_px"] > max_rms_reprojection_error_px:
        raise AppError(
            "ALIGNMENT_REPROJECTION_HIGH",
            "Robust board alignment was rejected because its RMS corner reprojection error exceeded the acceptance threshold.",
            status_code=422,
            suggested_action="Recapture sharper, well-covered board views or relax the threshold only after independent validation.",
            details={
                "rms_reprojection_error_px": solution["rms_reprojection_error_px"],
                "max_reprojection_error_px": solution["max_reprojection_error_px"],
                "threshold_px": max_rms_reprojection_error_px,
                "inlier_views": solution["inlier_view_count"],
                "corner_inliers": solution["corner_inlier_count"],
            },
        )
    
    rotation_matrix = np.array(solution["rotation"]).reshape(3, 3)
    from scipy.spatial.transform import Rotation
    quaternion = Rotation.from_matrix(rotation_matrix).as_quat().tolist()
    
    # Extract triangulated markers and update the board calibration layout in W frame
    try:
        triangulated = _extract_colmap_markers(container, project_id, sfm_dir, capture_id, float(board_def.get("marker_size_m", 0.035)))
        if triangulated:
            scale = float(solution["scale"])
            rot = rotation_matrix
            trans = np.array(solution["translation"], dtype=np.float64).reshape(3)
            real_layout = {}
            for tm in triangulated:
                corners_m0 = np.array(tm["corners"], dtype=np.float64)
                corners_w = (scale * (rot @ corners_m0.T)).T + trans
                real_layout[str(tm["marker_id"])] = corners_w.tolist()
            board_def = dict(board_def)
            board_def["layout"] = real_layout
            board_def["marker_ids"] = sorted([int(k) for k in real_layout.keys()])
            if board_def["marker_ids"]:
                board_def["anchor_id"] = board_def["marker_ids"][0]
            if board_calibration_id:
                try:
                    cal_res = container.catalog.get_resource(project_id, "aruco_board_calibration", board_calibration_id)
                    cal_res_payload = dict(cal_res)
                    cal_res_payload["board"] = board_def
                    artifact = container.artifacts.atomic_write_json(
                        container.artifacts.project_path(project_id, Path("calibrations/aruco-board") / f"{board_calibration_id}.json"),
                        cal_res_payload,
                    )
                    container.catalog.update_resource(project_id, "aruco_board_calibration", board_calibration_id, payload_patch={"board": board_def, "artifact": artifact})
                except Exception as ex:
                    print(f"[ALIGN MAP] Failed updating board cal: {ex}")
    except Exception as ex:
        print(f"[ALIGN MAP] Triangulation extraction error: {ex}")

    return container.catalog.update_resource(
        project_id, "scene_map", map_id, 
        payload_patch={
            "aruco_board_calibration_id": board_calibration_id,
            "board_definition": board_def,
            "similarity_s_w_m0": solution,
            "user_transform": {
                "position": solution["translation"],
                "quaternion": quaternion,
                "scale": solution["scale"]
            }
        }
    )

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
    response.headers["Cache-Control"] = "no-cache"
    data = json.loads(path.read_text(encoding="utf-8"))
    if scene_map.get("metric_binding"):
        data["metric_binding"] = scene_map["metric_binding"]
        data["published_coordinate_frame"] = "W"
        data["published_units"] = "m"
    user_transform = scene_map.get("user_transform")
    if isinstance(user_transform, dict):
        data["userTransform"] = user_transform
    capture_set_id = scene_map.get("parent_id") or scene_map.get("capture_set_id")
    cams = _extract_colmap_cameras(request.app.state.container, project_id, path.parent, capture_set_id)
    if cams:
        data["registered_cameras"] = cams
    nominal_size_m = 0.035
    board_cal_id = scene_map.get("aruco_board_calibration_id")
    board_markers: list[dict[str, Any]] = []
    if board_cal_id:
        try:
            b_res = request.app.state.container.catalog.get_resource(project_id, "aruco_board_calibration", board_cal_id)
            board_def = b_res.get("board", {})
            nominal_size_m = float(board_def.get("marker_size_m", nominal_size_m))
            data["board_definition"] = board_def
            
            layout = board_def.get("layout")
            sim = scene_map.get("similarity_s_w_m0")
            if isinstance(layout, dict) and isinstance(sim, dict) and sim.get("scale"):
                scale = float(sim["scale"])
                rot = np.array(sim["rotation"], dtype=np.float64).reshape(3, 3)
                trans = np.array(sim["translation"], dtype=np.float64).reshape(3)
                
                for m_id_str, corners_w in layout.items():
                    corners_w_arr = np.array(corners_w, dtype=np.float64).reshape(4, 3)
                    corners_m0 = ((corners_w_arr - trans) @ rot) / scale
                    center_m0 = np.mean(corners_m0, axis=0)
                    v1 = corners_m0[1] - corners_m0[0]
                    v2 = corners_m0[3] - corners_m0[0]
                    n_raw = np.cross(v1, v2)
                    n_val = np.linalg.norm(n_raw)
                    normal = (n_raw / n_val) if n_val > 1e-6 else np.array([0.0, 0.0, 1.0])
                    board_markers.append({
                        "id": int(m_id_str),
                        "marker_id": int(m_id_str),
                        "corners": [[float(coord) for coord in pt] for pt in corners_m0],
                        "center": [float(coord) for coord in center_m0],
                        "normal": [float(coord) for coord in normal],
                        "observation_count": int(sim.get("inlier_view_count", 1)),
                    })
        except Exception:
            pass

    triangulated_markers = _extract_colmap_markers(request.app.state.container, project_id, path.parent, capture_set_id, nominal_size_m)
    
    # Merge: Prioritize triangulated 3D markers from the SfM scene reconstruction
    merged_markers: list[dict[str, Any]] = list(triangulated_markers)
    triangulated_ids = {m["id"] for m in triangulated_markers}
    for bm in board_markers:
        if bm["id"] not in triangulated_ids:
            merged_markers.append(bm)
            triangulated_ids.add(bm["id"])

    if merged_markers:
        data["registered_markers"] = merged_markers
        sim = scene_map.get("similarity_s_w_m0")
        if sim and isinstance(sim, dict) and sim.get("scale"):
            scale = float(sim["scale"])
            rot = np.array(sim["rotation"], dtype=np.float64).reshape(3, 3)
            trans = np.array(sim["translation"], dtype=np.float64).reshape(3)
            real_layout = {}
            for tm in merged_markers:
                corners_m0 = np.array(tm["corners"], dtype=np.float64)
                corners_w = (scale * (rot @ corners_m0.T)).T + trans
                real_layout[str(tm["marker_id"])] = corners_w.tolist()
            if "board_definition" not in data or not isinstance(data.get("board_definition"), dict):
                data["board_definition"] = {}
            data["board_definition"]["layout"] = real_layout
            data["board_definition"]["marker_ids"] = sorted([int(k) for k in real_layout.keys()])
            data["board_definition"]["dictionary"] = "DICT_4X4_50"
            data["board_definition"]["marker_size_m"] = nominal_size_m
    return data


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


def _serve_frame_as_jpeg(request: Request, frame: dict[str, Any]) -> Response:
    rgb_art = frame.get("rgb_artifact")
    if not rgb_art:
        raise AppError("FRAME_NO_IMAGE", "Frame has no RGB image data.", status_code=404)
    rgb_path = request.app.state.container.artifacts.root / rgb_art["relative_uri"]
    if not rgb_path.is_file():
        raise AppError("FRAME_IMAGE_MISSING", "Frame image file is missing from disk.", status_code=404)
    w = int(frame["width"])
    h = int(frame["height"])
    rgb_bytes = rgb_path.read_bytes()
    import numpy as np
    arr = np.frombuffer(rgb_bytes, dtype=np.uint8).reshape(h, w, 3)
    try:
        import io
        from PIL import Image  # type: ignore[import-untyped]
        img = Image.fromarray(arr, "RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return Response(content=buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    except ImportError:
        import io, struct, zlib  # noqa: E401
        def _png(arr: np.ndarray) -> bytes:
            rows = []
            for row in arr:
                rows.append(b"\x00" + row.tobytes())
            raw = b"".join(rows)
            compressed = zlib.compress(raw, 6)
            def chunk(tag: bytes, data: bytes) -> bytes:
                c = struct.pack(">I", len(data)) + tag + data
                return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            sig = b"\x89PNG\r\n\x1a\n"
            ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            idat = chunk(b"IDAT", compressed)
            iend = chunk(b"IEND", b"")
            return sig + ihdr + idat + iend
        return Response(content=_png(arr), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/projects/{project_id}/frames/by-id/{frame_id}/image")
def frame_image_by_id(request: Request, project_id: str, frame_id: str) -> Response:
    """Serve a capture frame as JPEG looked up directly by its resource frame_id."""
    container = request.app.state.container
    frame = container.catalog.get_resource(project_id, "capture_frame", frame_id)
    if not frame:
        raise AppError("FRAME_NOT_FOUND", f"No frame found with id '{frame_id}'.", status_code=404)
    return _serve_frame_as_jpeg(request, frame)


@router.get("/projects/{project_id}/frames/{frame_name}/image")
def frame_image(request: Request, project_id: str, frame_name: str) -> Response:
    """Serve a capture frame as JPEG looked up by camera frame name (e.g. frame-000003.png)."""
    stem = Path(frame_name).stem  # e.g. 'frame-000003'
    container = request.app.state.container
    capture_sets = container.catalog.list_resources(project_id, "capture_set", limit=100)
    if not capture_sets:
        raise AppError("CAPTURE_SET_NOT_FOUND", "No capture sets found.", status_code=404)

    # Prioritize active map's capture set so we search the correct recording session
    active_map_id = container.catalog.get_project(project_id).get("active_map_id")
    target_cset_id = None
    if active_map_id:
        try:
            scene_map = container.catalog.get_resource(project_id, "scene_map", active_map_id)
            target_cset_id = scene_map.get("parent_id") or scene_map.get("capture_set_id")
        except Exception:
            target_cset_id = None

    if target_cset_id:
        capture_sets.sort(key=lambda cs: 0 if cs["id"] == target_cset_id else 1)

    target_index: int | None = None
    try:
        target_index = int(stem.split("-")[-1])
    except (ValueError, IndexError):
        target_index = None

    target_frame = None
    for cset in capture_sets:
        raw_frames = [f for f in container.catalog.list_resources(project_id, "capture_frame", parent_id=cset["id"], limit=2000) if f.get("included", True)]
        if target_index is not None and 0 <= target_index < len(raw_frames):
            target_frame = raw_frames[target_index]
        else:
            for f in raw_frames:
                if f.get("id") == frame_name or f.get("name") == frame_name:
                    target_frame = f
                    break
        if target_frame:
            break
    if not target_frame:
        raise AppError("FRAME_NOT_FOUND", f"No frame found for '{frame_name}'.", status_code=404)
    return _serve_frame_as_jpeg(request, target_frame)


@router.get("/projects/{project_id}/maps/{map_id}/openmvs/{filename:path}")
def map_openmvs_file(request: Request, project_id: str, map_id: str, filename: str) -> FileResponse:
    """Serve files generated by OpenMVS like meshes, materials, and textures."""
    container = request.app.state.container
    map_record = container.catalog.get_resource(project_id, "scene_map", map_id)
    if not map_record:
        raise AppError("MAP_NOT_FOUND", f"Map {map_id} not found.", status_code=404)
        
    map_dir = container.artifacts.project_path(project_id, Path("maps") / map_id)
    file_path = map_dir / "openmvs" / filename
    
    if not file_path.is_file():
        raise AppError("FILE_NOT_FOUND", f"OpenMVS file {filename} not found in map {map_id}.", status_code=404)
        
    return FileResponse(file_path)
