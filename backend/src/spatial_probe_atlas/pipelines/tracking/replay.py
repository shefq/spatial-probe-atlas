from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from spatial_probe_atlas.domain.transforms import compose_tip


CAMERA_MIN_INLIERS = 15
CAMERA_MAX_REPROJECTION_ERROR_PX = 8.0
PROBE_MIN_INLIERS = 4
PROBE_MAX_REPROJECTION_ERROR_PX = 2.5
MAX_TRACKING_LATENCY_MS = 150.0


def make_replay_tracking_frame(session_id: str, sequence: int, t_marker_tip: list[float] | None = None) -> dict[str, Any]:
    # Runtime dispatch keeps the deterministic adapter explicit while giving a connected
    # Record3D device the real SIFT/depth localization + blob/PnP implementation.
    try:
        from .runtime import real_tracking_frame
        real = real_tracking_frame(session_id)
        if real is not None:
            return real
    except Exception as exc:
        return {
            "session_id": session_id, "frame_id": sequence, "device_timestamp_ns": time.monotonic_ns(),
            "camera_state": "lost", "probe_state": "lost", "t_w_c": None, "t_c_m": None, "tip_w_m": None,
            "camera_inliers": 0, "camera_reprojection_error_px": None, "probe_inliers": 0,
            "probe_reprojection_error_px": None, "fps": 0.0, "latency_ms": 0.0, "quality": "lost",
            "rejection_reasons": ["cpu_tracking_pipeline_error", str(exc)], "coordinate_frame": "W", "units": "m", "simulated": False,
        }
    t_w_c = np.eye(4)
    t_w_c[0, 3] = sequence * 0.0004
    t_w_c[1, 3] = 0.005 * math.sin(sequence / 20)
    t_c_m = np.eye(4)
    t_c_m[:3, 3] = [0.01, 0.02, 0.19]
    t_m_p = t_marker_tip or [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, -0.1, 0, 0, 0, 1]
    t_w_p = compose_tip(t_w_c.reshape(-1).tolist(), t_c_m.reshape(-1).tolist(), t_m_p)
    tip = np.asarray(t_w_p, dtype=float).reshape(4, 4)[:3, 3].tolist()
    now = time.monotonic_ns()
    return {
        "session_id": session_id, "frame_id": sequence, "device_timestamp_ns": now, "server_timestamp_ns": now,
        "camera_state": "tracked", "probe_state": "tracked", "t_w_c": t_w_c.reshape(-1).tolist(),
        "t_c_m": t_c_m.reshape(-1).tolist(), "t_w_p": t_w_p, "tip_w_m": tip,
        "camera_inliers": 142, "camera_reprojection_error_px": 1.37, "probe_inliers": 5,
        "probe_reprojection_error_px": 0.91, "fps": 20.0, "latency_ms": 3.0, "quality": "good",
        "coordinate_frame": "W", "units": "m", "simulated": True,
    }


def quality_gate(frame: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    is_aruco = bool(frame.get("is_aruco_mode") or frame.get("camera_inliers_is_aruco"))
    min_cam_inliers = 4 if is_aruco else CAMERA_MIN_INLIERS
    if frame.get("camera_state") != "tracked" or int(frame.get("camera_inliers", 0)) < min_cam_inliers or float(frame.get("camera_reprojection_error_px") or math.inf) > CAMERA_MAX_REPROJECTION_ERROR_PX:
        reasons.append("camera_localization_quality")
    if frame.get("probe_state") != "tracked" or int(frame.get("probe_inliers", 0)) < PROBE_MIN_INLIERS or float(frame.get("probe_reprojection_error_px") or math.inf) > PROBE_MAX_REPROJECTION_ERROR_PX:
        reasons.append("probe_tracking_quality")
    if float(frame.get("latency_ms", math.inf)) > MAX_TRACKING_LATENCY_MS:
        reasons.append("tracking_latency")
    position = frame.get("tip_w_m")
    if not isinstance(position, list) or len(position) != 3 or not np.isfinite(position).all():
        reasons.append("tip_transform")
    return not reasons, reasons
