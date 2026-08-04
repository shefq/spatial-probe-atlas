from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter, Request

from spatial_probe_atlas.api.schemas import MapCreate
from spatial_probe_atlas.compute import probe_cuda, resolve_mapping_profile
from spatial_probe_atlas.domain.errors import AppError


router = APIRouter()


@router.post("/projects/{project_id}/maps", status_code=202)
def create_map_with_frozen_profile(request: Request, project_id: str, body: MapCreate) -> dict[str, Any]:
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
    capability = getattr(container, "cuda_capability", None)
    if capability is None:
        capability = probe_cuda(container.settings.data_root / "models")
        container.cuda_capability = capability
    replay_only = bool(frames) and all(frame.get("source") == "replay" for frame in frames)
    effective = resolve_mapping_profile(body.compute_profile, capability, replay_only=replay_only)
    scene_map = container.catalog.create_resource(
        project_id,
        "scene_map",
        state="building",
        name=body.name,
        parent_id=body.capture_set_id,
        payload={
            "capture_set_id": body.capture_set_id,
            "capture_set_revision": capture_set["revision"],
            "requested_compute_profile": body.compute_profile,
            "effective_compute_profile": effective,
            "point_count": 0,
            "coordinate_frame": "M0",
            "units": "arbitrary",
        },
    )
    frame_ids = [frame["frame_id"] for frame in frames]
    job = container.catalog.create_job(
        project_id=project_id,
        owner_id=scene_map["map_id"],
        type="mapping",
        spec={
            "stage_count": 10,
            "frame_ids": frame_ids,
            "capture_set_id": body.capture_set_id,
            "requested_compute_profile": body.compute_profile,
            "effective_compute_profile": effective,
        },
    )
    container.artifacts.atomic_write_json(
        container.artifacts.staging / job["job_id"] / "mapping-profile.json",
        {"schema_version": 1, "requested_compute_profile": body.compute_profile, "effective_compute_profile": effective},
    )
    container.catalog.update_resource(project_id, "capture_set", body.capture_set_id, state="processing", payload_patch={"frozen_revision": capture_set["revision"]})
    container.jobs.submit(job["job_id"])
    return {
        "id": scene_map["map_id"], "map_id": scene_map["map_id"], "project_id": project_id,
        "name": body.name, "state": "queued", "active": False, "capture_set_id": body.capture_set_id,
        "capture_set_revision": capture_set["revision"], "point_count": 0, "job_id": job["job_id"],
        "requested_compute_profile": body.compute_profile, "effective_compute_profile": effective,
        "coordinate_frame": "M0", "units": "arbitrary", "created_at": scene_map["created_at"],
    }
