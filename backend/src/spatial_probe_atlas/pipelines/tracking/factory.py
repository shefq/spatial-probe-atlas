from __future__ import annotations

from pathlib import Path
from typing import Any

from spatial_probe_atlas.domain.errors import AppError


def create_tracking_pipeline(scene_map: dict[str, Any] | None, similarity: dict[str, Any], calibration: dict[str, Any], artifact_root: Path, registration: dict[str, Any] | None = None) -> Any:
    if registration and registration.get("is_aruco_mode"):
        from .aruco import ArucoTrackingPipeline
        return ArucoTrackingPipeline(registration, calibration)

    if not scene_map:
        raise AppError("SCENE_MAP_REQUIRED", "Scene map is required for non-ArUco tracking modes.", status_code=409)

    sfm_info = scene_map.get("sfm") or {}
    sfm_uri = sfm_info.get("relative_uri")
    has_aliked_npz = False
    if sfm_uri:
        npz_file = (artifact_root / Path(sfm_uri)).resolve() / "aliked_features.npz"
        has_aliked_npz = npz_file.is_file()

    if has_aliked_npz:
        from .cuda_lightglue import LightGlueTrackingPipeline
        return LightGlueTrackingPipeline(sfm_info, similarity, calibration, artifact_root)

    index = scene_map.get("localization_index") or {}
    if not index.get("relative_uri"):
        raise AppError("LOCALIZATION_INDEX_UNAVAILABLE", "The active map has neither ALIKED features nor a SIFT localization index.", status_code=409)

    from .indexed import IndexedCpuTrackingPipeline
    return IndexedCpuTrackingPipeline(index, similarity, calibration, artifact_root)
