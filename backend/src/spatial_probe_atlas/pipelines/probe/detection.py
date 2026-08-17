from __future__ import annotations

from typing import Any

import numpy as np

from spatial_probe_atlas.domain.validation import validate_blob_detector


DEFAULT_BLOB_DETECTOR: dict[str, Any] = {
    "minThreshold": 61.0, "maxThreshold": 169.0, "thresholdStep": 17.0,
    "minRepeatability": 2, "minDistBetweenBlobs": 10.0,
    "maxReprojectionError": 2.5,
    "filterByColor": True, "blobColor": 0,
    "filterByArea": True, "minArea": 50.0, "maxArea": 1261.0,
    "filterByCircularity": True, "minCircularity": 0.57, "maxCircularity": 1.0,
    "filterByInertia": True, "minInertiaRatio": 0.10, "maxInertiaRatio": 1.0,
    "filterByConvexity": False, "minConvexity": 0.87, "maxConvexity": 1.0,
}


import itertools
import math

PROBE_POINTS = [
    [-0.005, 0.0, 0.0], [-0.01475, -0.04035, 0.04518], [-0.02373, 0.04438, 0.03497],
    [-0.00672, -0.00053, -0.05909], [-0.01971, 0.03488, -0.02480],
]


_BLOB_DETECTOR_CACHE: dict[tuple, Any] = {}

def _get_blob_detector(cv2: Any, settings: dict[str, Any]) -> Any:
    key_fields = ("minThreshold", "maxThreshold", "thresholdStep", "minRepeatability", "minDistBetweenBlobs", "blobColor", "filterByColor", "minArea", "maxArea", "filterByArea", "minCircularity", "maxCircularity", "filterByCircularity", "minInertiaRatio", "maxInertiaRatio", "filterByInertia", "minConvexity", "maxConvexity", "filterByConvexity")
    cache_key = tuple(settings.get(f) for f in key_fields)
    if cache_key not in _BLOB_DETECTOR_CACHE:
        params = cv2.SimpleBlobDetector_Params()
        for field in ("minThreshold", "maxThreshold", "thresholdStep", "minDistBetweenBlobs", "minArea", "maxArea", "minCircularity", "maxCircularity", "minInertiaRatio", "maxInertiaRatio", "minConvexity", "maxConvexity"):
            setattr(params, field, float(settings[field]))
        params.minRepeatability = int(settings["minRepeatability"])
        if params.maxThreshold <= params.minThreshold + params.thresholdStep:
            params.minRepeatability = 1
        params.blobColor = int(settings["blobColor"])
        for field in ("filterByColor", "filterByArea", "filterByCircularity", "filterByInertia", "filterByConvexity"):
            setattr(params, field, bool(settings[field]))
        _BLOB_DETECTOR_CACHE[cache_key] = cv2.SimpleBlobDetector_create(params)
    return _BLOB_DETECTOR_CACHE[cache_key]


def detect_blobs(
    rgb: bytes | np.ndarray,
    width: int,
    height: int,
    settings: dict[str, Any],
    intrinsic_matrix: list[float] | np.ndarray | None = None,
    marker_points_m: list[list[float]] | np.ndarray | None = None,
    gray_image: np.ndarray | None = None,
) -> dict[str, Any]:
    errors = validate_blob_detector(settings)
    if errors:
        return {"candidate_count": 0, "tracked": False, "errors": errors, "keypoints": []}
    try:
        import cv2  # type: ignore
    except Exception:
        return {"candidate_count": 0, "tracked": False, "errors": ["opencv_not_available"], "keypoints": []}
        
    if gray_image is not None:
        gray = gray_image
    elif isinstance(rgb, np.ndarray):
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY) if rgb.ndim == 3 else rgb
    else:
        image = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        
    detector = _get_blob_detector(cv2, settings)
    keypoints = detector.detect(gray)
    points = [{"x": float(item.pt[0]), "y": float(item.pt[1]), "diameter": float(item.size), "response": float(item.response)} for item in keypoints]

    if len(points) < 5:
        return {"candidate_count": len(points), "tracked": False, "errors": [], "keypoints": points}

    points_sorted = sorted(points, key=lambda p: p["diameter"], reverse=True)
    candidates = np.asarray([[p["x"], p["y"]] for p in points_sorted[:6]], dtype=np.float32)
    if marker_points_m is not None and len(marker_points_m) == 5:
        object_points = np.asarray(marker_points_m, dtype=np.float32)
    else:
        object_points = np.asarray(PROBE_POINTS, dtype=np.float32)

    if intrinsic_matrix is not None:
        k = np.asarray(intrinsic_matrix, dtype=float).reshape(3, 3)
    else:
        k = np.array([[width * 0.8, 0, width / 2.0], [0, width * 0.8, height / 2.0], [0, 0, 1.0]], dtype=float)

    max_err = float(settings.get("maxReprojectionError", 2.5))
    best_err = math.inf
    best_points = points
    best_rvec = None
    best_tvec = None

    for selection in itertools.combinations(range(len(candidates)), 5):
        sub_pts = candidates[list(selection)]
        for perm in itertools.permutations(range(5)):
            ordered = sub_pts[list(perm)]
            success, rvec, tvec = cv2.solvePnP(object_points, ordered, k, None, flags=cv2.SOLVEPNP_EPNP)
            if not success or tvec[2, 0] <= 0:
                continue
            projected, _ = cv2.projectPoints(object_points, rvec, tvec, k, None)
            error = float(np.sqrt(np.mean(np.sum((projected[:, 0] - ordered) ** 2, axis=1))))
            if error < best_err:
                best_err = error
                best_points = [points_sorted[i] for i in selection]
                best_rvec = rvec.copy()
                best_tvec = tvec.copy()

    tracked = math.isfinite(best_err) and best_err <= max_err
    result: dict[str, Any] = {
        "candidate_count": len(points),
        "tracked": tracked,
        "errors": [],
        "keypoints": best_points if tracked else points,
        "reprojection_error_px": best_err if tracked else None,
    }
    if tracked and best_rvec is not None and best_tvec is not None:
        result["rvec"] = best_rvec.tolist()
        result["tvec"] = best_tvec.tolist()
    return result

