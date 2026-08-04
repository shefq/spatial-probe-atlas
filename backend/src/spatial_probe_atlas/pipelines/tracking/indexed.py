from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np

from spatial_probe_atlas.domain.errors import AppError

from .cpu import CpuTrackingPipeline, TrackingState


class IndexedCpuTrackingPipeline(CpuTrackingPipeline):
    """PnP localizer backed by the exact SIFT descriptors/points published by mapping."""

    def __init__(self, localization_index: dict[str, Any], similarity: dict[str, Any], calibration: dict[str, Any], artifact_root: Path) -> None:
        try:
            import cv2  # type: ignore
        except Exception as exc:
            raise AppError("CPU_TRACKING_UNAVAILABLE", "OpenCV SIFT is required for CPU tracking.", status_code=503) from exc
        uri = localization_index.get("relative_uri")
        checksum = localization_index.get("sha256")
        if not isinstance(uri, str) or not isinstance(checksum, str):
            raise AppError("LOCALIZATION_INDEX_UNAVAILABLE", "The active map has no checksum-bound localization index.", status_code=409)
        root = artifact_root.resolve()
        path = (root / Path(uri)).resolve()
        if root not in path.parents or not path.is_file():
            raise AppError("LOCALIZATION_INDEX_MISSING", "The active map localization index is missing.", status_code=409)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != checksum:
            raise AppError("LOCALIZATION_INDEX_CHECKSUM_FAILED", "The active map localization index failed checksum validation.", status_code=500)
        try:
            with np.load(path, allow_pickle=False) as values:
                schema_version = int(np.asarray(values["schema_version"]).reshape(-1)[0])
                points_m0 = np.asarray(values["points_m0"], dtype=np.float32)
                descriptors = np.asarray(values["descriptors"], dtype=np.float32)
        except Exception as exc:
            raise AppError("LOCALIZATION_INDEX_INVALID", "The active map localization index is not a valid v1 NPZ artifact.", status_code=422) from exc
        if schema_version != 1 or points_m0.ndim != 2 or points_m0.shape[1] != 3 or descriptors.shape != (len(points_m0), 128):
            raise AppError("LOCALIZATION_INDEX_INVALID", "The active map localization index has invalid dimensions.", status_code=422)
        if len(points_m0) < 30 or not np.isfinite(points_m0).all() or not np.isfinite(descriptors).all():
            raise AppError("LOCALIZATION_INDEX_INSUFFICIENT", "The active map localization index has fewer than 30 finite correspondences.", status_code=422)
        try:
            scale = float(similarity["scale"])
            rotation = np.asarray(similarity["rotation"], dtype=float).reshape(3, 3)
            translation = np.asarray(similarity["translation"], dtype=float).reshape(3)
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError("REGISTRATION_SIMILARITY_INVALID", "Tracking requires the active registration similarity.", status_code=409) from exc
        if not np.isfinite(scale) or scale <= 0 or not np.isfinite(rotation).all() or not np.isfinite(translation).all():
            raise AppError("REGISTRATION_SIMILARITY_INVALID", "Tracking requires a finite positive registration similarity.", status_code=409)
        points_w = (scale * (rotation @ points_m0.T)).T + translation
        self.cv2 = cv2
        self.sift = cv2.SIFT_create(nfeatures=3000)
        self.matcher = cv2.BFMatcher(cv2.NORM_L2)
        self.references = [{"descriptors": descriptors, "world_points": np.asarray(points_w, dtype=np.float32)}]
        self.calibration = calibration
        self.camera_state = TrackingState()
        self.probe_state = TrackingState()
