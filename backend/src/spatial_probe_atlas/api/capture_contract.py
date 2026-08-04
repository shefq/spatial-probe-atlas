from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from spatial_probe_atlas.api.contract import _capture_summary
from spatial_probe_atlas.api.projects_mapping import capture_frames as capture_frames_command
from spatial_probe_atlas.api.schemas import CaptureFramesRequest


router = APIRouter()


@router.post("/projects/{project_id}/capture-sets/{capture_set_id}/frames:capture", status_code=201)
async def capture_frames_stable_shape(request: Request, project_id: str, capture_set_id: str, body: CaptureFramesRequest | None = None) -> Any:
    result = await capture_frames_command(request, project_id, capture_set_id, body or CaptureFramesRequest())
    items = [_capture_summary(item) for item in result["items"]]
    # Preserve the ergonomic single-frame browser response while making a requested batch
    # an explicit envelope with the updated capture-set revision.
    if len(items) == 1:
        return items[0]
    return {"items": items, "count": len(items), "capture_set": result["capture_set"]}
