from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import numpy as np

from .errors import AppError


WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))
}


def validate_project_name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
        raise AppError("PROJECT_NAME_INVALID", "Project names must contain 1–80 visible characters.", status_code=422)
    if name.rstrip(". ").upper() in WINDOWS_RESERVED or re.search(r'[<>:"/\\|?*]', name):
        raise AppError("PROJECT_NAME_INVALID", "The project name is not valid on Windows.", status_code=422)
    return name


def finite_numbers(values: list[Any], *, expected: int | None = None, field: str = "value") -> None:
    if expected is not None and len(values) != expected:
        raise AppError("SEMANTIC_VALIDATION_FAILED", f"{field} must contain {expected} values.", status_code=422)
    if any(not isinstance(x, (int, float)) or not math.isfinite(float(x)) for x in values):
        raise AppError("SEMANTIC_VALIDATION_FAILED", f"{field} must contain only finite numbers.", status_code=422)


def validate_blob_detector(blob: dict[str, Any]) -> list[str]:
    required = {
        "minThreshold", "maxThreshold", "thresholdStep", "minRepeatability", "minDistBetweenBlobs",
        "filterByColor", "blobColor", "filterByArea", "minArea", "maxArea", "filterByCircularity",
        "minCircularity", "maxCircularity", "filterByInertia", "minInertiaRatio", "maxInertiaRatio",
        "filterByConvexity", "minConvexity", "maxConvexity",
    }
    missing = sorted(required - blob.keys())
    errors: list[str] = [f"blob_detector.{key} is required" for key in missing]
    if missing:
        return errors
    if not 0 <= float(blob["minThreshold"]) < float(blob["maxThreshold"]) <= 255:
        errors.append("minThreshold must be less than maxThreshold and both must be within 0..255")
    if not 0 < float(blob["thresholdStep"]) <= 255:
        errors.append("thresholdStep must be within (0,255]")
    if int(blob["minRepeatability"]) < 1:
        errors.append("minRepeatability must be at least 1")
    if float(blob["minDistBetweenBlobs"]) < 0:
        errors.append("minDistBetweenBlobs must not be negative")
    if "maxReprojectionError" in blob and float(blob["maxReprojectionError"]) <= 0:
        errors.append("maxReprojectionError must be positive")
    if not 0 <= int(blob["blobColor"]) <= 255:
        errors.append("blobColor must be within 0..255")
    for enabled, low, high in (
        ("filterByArea", "minArea", "maxArea"),
        ("filterByCircularity", "minCircularity", "maxCircularity"),
        ("filterByInertia", "minInertiaRatio", "maxInertiaRatio"),
        ("filterByConvexity", "minConvexity", "maxConvexity"),
    ):
        lo, hi = float(blob[low]), float(blob[high])
        if bool(blob[enabled]) and lo > hi:
            errors.append(f"{low} must be less than or equal to {high}")
        if enabled != "filterByArea" and not (0 <= lo <= 1 and 0 <= hi <= 1):
            errors.append(f"{low} and {high} must be within 0..1")
        if enabled == "filterByArea" and (lo < 0 or hi <= 0):
            errors.append("area bounds must be non-negative and maxArea positive")
    return errors


def validate_probe_calibration(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        if data.get("schema_version") != "1.0.0":
            errors.append("schema_version must be 1.0.0")
        if data.get("units") != "m":
            errors.append("units must be m")
        probe = data["probe"]
        points = np.asarray(probe["marker_points_m"], dtype=float)
        if points.shape != (5, 3) or not np.isfinite(points).all():
            errors.append("probe.marker_points_m must contain five finite 3D points")
        elif len(np.unique(np.round(points, 9), axis=0)) != 5 or np.linalg.matrix_rank(points - points.mean(axis=0)) < 2:
            errors.append("probe marker geometry must contain five unique, non-degenerate points")
        elif float(np.max(np.linalg.norm(points[:, None] - points[None, :], axis=2))) > 0.5:
            errors.append("probe marker geometry exceeds the supported 0.5 m scale")
        transform = np.asarray(probe["t_marker_tip"], dtype=float).reshape(4, 4)
        if not np.isfinite(transform).all() or not np.allclose(transform[3], [0, 0, 0, 1], atol=1e-8):
            errors.append("probe.t_marker_tip must be a finite homogeneous transform")
        elif not np.allclose(transform[:3, :3].T @ transform[:3, :3], np.eye(3), atol=1e-4) or np.linalg.det(transform[:3, :3]) < 0.999:
            errors.append("probe.t_marker_tip rotation must be rigid and right-handed")
        errors.extend(validate_blob_detector(data["blob_detector"]))
        quality = data["quality"]
        if int(quality["accepted_frame_count"]) > int(quality["input_frame_count"]):
            errors.append("accepted_frame_count cannot exceed input_frame_count")
        if float(quality["rms_reprojection_error_px"]) < 0:
            errors.append("rms_reprojection_error_px cannot be negative")
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"invalid calibration structure: {exc}")
    return errors


def validate_camera_calibration(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        if int(data["image_width"]) <= 0 or int(data["image_height"]) <= 0:
            errors.append("image dimensions must be positive")
        k = np.asarray(data["intrinsic_matrix"], dtype=float).reshape(3, 3)
        if not np.isfinite(k).all() or k[0, 0] <= 0 or k[1, 1] <= 0 or not np.allclose(k[2], [0, 0, 1], atol=1e-8):
            errors.append("intrinsic_matrix is not a valid pinhole K matrix")
        if not (0 <= k[0, 2] <= int(data["image_width"]) and 0 <= k[1, 2] <= int(data["image_height"])):
            errors.append("principal point is outside the image")
        model = data["distortion_model"]
        count = len(data["distortion_coefficients"])
        valid_counts = {"none": {0}, "plumb_bob": {4, 5}, "rational_polynomial": {8, 12, 14}, "equidistant": {4}}
        if model not in valid_counts or count not in valid_counts[model]:
            errors.append("distortion coefficient count does not match distortion_model")
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"invalid camera calibration structure: {exc}")
    return errors


def safe_relative_path(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise AppError("PATH_OUTSIDE_DATA_ROOT", "The requested artifact path is not allowed.", status_code=400) from exc
    if any(parent.is_symlink() for parent in [resolved, *resolved.parents] if parent != resolved_root):
        raise AppError("SYMLINK_NOT_ALLOWED", "Artifact paths may not escape through symbolic links.", status_code=400)
    return resolved

