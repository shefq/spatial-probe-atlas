from __future__ import annotations

from fastapi import APIRouter

from spatial_probe_atlas.domain.errors import AppError


router = APIRouter()


@router.api_route("/{unmatched_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"], include_in_schema=False)
def api_not_found(unmatched_path: str) -> None:
    raise AppError("API_ROUTE_NOT_FOUND", "The requested API route does not exist.", status_code=404, details={"path": f"/api/v1/{unmatched_path}"})
