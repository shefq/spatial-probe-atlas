from __future__ import annotations

from typing import Any

import numpy as np

from spatial_probe_atlas.domain.validation import validate_blob_detector


DEFAULT_BLOB_DETECTOR: dict[str, Any] = {
    "minThreshold": 61.0, "maxThreshold": 169.0, "thresholdStep": 17.0,
    "minRepeatability": 2, "minDistBetweenBlobs": 10.0,
    "filterByColor": True, "blobColor": 0,
    "filterByArea": True, "minArea": 50.0, "maxArea": 1261.0,
    "filterByCircularity": True, "minCircularity": 0.57, "maxCircularity": 1.0,
    "filterByInertia": True, "minInertiaRatio": 0.10, "maxInertiaRatio": 1.0,
    "filterByConvexity": False, "minConvexity": 0.87, "maxConvexity": 1.0,
}


def detect_blobs(rgb: bytes, width: int, height: int, settings: dict[str, Any]) -> dict[str, Any]:
    errors = validate_blob_detector(settings)
    if errors:
        return {"candidate_count": 0, "tracked": False, "errors": errors, "keypoints": []}
    try:
        import cv2  # type: ignore
    except Exception:
        return {"candidate_count": 0, "tracked": False, "errors": ["opencv_not_available"], "keypoints": []}
    image = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    params = cv2.SimpleBlobDetector_Params()
    for field in ("minThreshold", "maxThreshold", "thresholdStep", "minDistBetweenBlobs", "minArea", "maxArea", "minCircularity", "maxCircularity", "minInertiaRatio", "maxInertiaRatio", "minConvexity", "maxConvexity"):
        setattr(params, field, float(settings[field]))
    params.minRepeatability = int(settings["minRepeatability"])
    params.blobColor = int(settings["blobColor"])
    for field in ("filterByColor", "filterByArea", "filterByCircularity", "filterByInertia", "filterByConvexity"):
        setattr(params, field, bool(settings[field]))
    keypoints = cv2.SimpleBlobDetector_create(params).detect(gray)
    points = [{"x": float(item.pt[0]), "y": float(item.pt[1]), "diameter": float(item.size), "response": float(item.response)} for item in keypoints]
    return {"candidate_count": len(points), "tracked": len(points) == 5, "errors": [], "keypoints": points}

