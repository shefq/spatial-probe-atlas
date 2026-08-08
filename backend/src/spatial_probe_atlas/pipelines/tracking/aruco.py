from __future__ import annotations

import math
from typing import Any

import numpy as np

from spatial_probe_atlas.ports.camera import NormalizedCameraFrame

from .cpu import CpuTrackingPipeline, TrackingState


class ArucoTrackingPipeline(CpuTrackingPipeline):
    """PnP localizer backed by ArUco marker tracking, bypassing ALIKED/LightGlue."""

    def __init__(self, registration: dict[str, Any], calibration: dict[str, Any]) -> None:
        try:
            import cv2  # type: ignore
        except Exception as exc:
            from spatial_probe_atlas.domain.errors import AppError
            raise AppError("ARUCO_TRACKING_UNAVAILABLE", "OpenCV is required for ArUco tracking.", status_code=503) from exc
            
        self.cv2 = cv2
        self.calibration = calibration
        self.camera_state = TrackingState()
        self.probe_state = TrackingState()
        
        board_def = registration.get("board_definition", {})
        self.dictionary_name = board_def.get("dictionary", "DICT_4X4_50")
        self.marker_ids = board_def.get("marker_ids", [])
        self.anchor_id = board_def.get("anchor_id", 7)
        self.marker_size_m = float(board_def.get("marker_size_m", 0.020))
        
        layout = board_def.get("layout", {})
        self.marker_layout = {int(k): np.asarray(v, dtype=np.float64) for k, v in layout.items()}
        self.references = []

    def _localize(self, frame: NormalizedCameraFrame) -> tuple[np.ndarray | None, int, float | None, str | None]:
        from spatial_probe_atlas.pipelines.aruco import detect_aruco, estimate_board_pose, matrix_from_pose
        
        detections, _ = detect_aruco(frame.rgb, frame.width, frame.height, self.dictionary_name, self.marker_ids)
        if not detections:
            return None, 0, math.inf, "no_aruco_markers_detected"
            
        K = np.asarray(frame.intrinsic_matrix, dtype=float).reshape(3, 3)
        detections_np = {k: np.asarray(v) for k, v in detections.items()}
        
        pose, used_ids = estimate_board_pose(self.marker_layout, detections_np, K, minimum_markers=1)
        if pose is None:
            return None, len(used_ids) * 4, math.inf, "aruco_pnp_failed"
            
        t_c_w = matrix_from_pose(pose.rvec, pose.tvec)
        t_w_c = np.linalg.inv(t_c_w)
        
        inliers = len(used_ids) * 4
        return t_w_c, inliers, float(pose.reprojection_error_px), None

    localize_camera = _localize

