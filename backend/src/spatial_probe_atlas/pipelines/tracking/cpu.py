from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from spatial_probe_atlas.domain.transforms import compose_tip
from spatial_probe_atlas.pipelines.probe import detect_blobs
from spatial_probe_atlas.ports.camera import NormalizedCameraFrame

from .replay import (
    CAMERA_MAX_REPROJECTION_ERROR_PX,
    CAMERA_MIN_INLIERS,
    MAX_TRACKING_LATENCY_MS,
    PROBE_MAX_REPROJECTION_ERROR_PX,
    PROBE_MIN_INLIERS,
)


MAX_TRANSLATION_JUMP_M = 0.20
MAX_ROTATION_JUMP_DEG = 60.0
POSE_EMA_ALPHA = 0.35
LOST_AFTER_REJECTED_FRAMES = 5
RECOVER_AFTER_GOOD_FRAMES = 1


@dataclass
class TrackingState:
    accepted_pose: np.ndarray | None = None
    rejected_count: int = 0
    good_count: int = 0
    state: str = "lost"

    def update(self, pose: np.ndarray | None) -> tuple[str, np.ndarray | None, str | None]:
        if pose is None:
            self.rejected_count += 1
            self.good_count = 0
            if self.rejected_count >= LOST_AFTER_REJECTED_FRAMES:
                self.state = "lost"
                self.accepted_pose = None
            return self.state, self.accepted_pose, "pose_rejected"
        if self.state == "lost":
            self.accepted_pose = None
        if self.accepted_pose is not None:
            delta = np.linalg.inv(self.accepted_pose) @ pose
            translation = float(np.linalg.norm(delta[:3, 3]))
            angle = math.degrees(math.acos(float(np.clip((np.trace(delta[:3, :3]) - 1) / 2, -1, 1))))
            if translation > MAX_TRANSLATION_JUMP_M or angle > MAX_ROTATION_JUMP_DEG:
                self.rejected_count += 1
                self.good_count = 0
                if self.rejected_count >= LOST_AFTER_REJECTED_FRAMES:
                    self.state = "lost"
                    self.accepted_pose = None
                return self.state, self.accepted_pose, "implausible_pose_jump"
            filtered = pose.copy()
            filtered[:3, 3] = (1 - POSE_EMA_ALPHA) * self.accepted_pose[:3, 3] + POSE_EMA_ALPHA * pose[:3, 3]
            # Re-orthogonalize linear rotation blend.
            blended = (1 - POSE_EMA_ALPHA) * self.accepted_pose[:3, :3] + POSE_EMA_ALPHA * pose[:3, :3]
            u, _, vt = np.linalg.svd(blended)
            filtered[:3, :3] = u @ vt
            pose = filtered
        self.accepted_pose = pose
        self.rejected_count = 0
        self.good_count += 1
        if self.state == "tracked" or self.good_count >= RECOVER_AFTER_GOOD_FRAMES:
            self.state = "tracked"
        return self.state, pose, None


class CpuTrackingPipeline:
    """Real CPU SIFT+depth reference localization and five-marker PnP path."""

    def __init__(self, references: list[dict[str, Any]], similarity: dict[str, Any] | None, calibration: dict[str, Any], artifact_root: Any) -> None:
        import cv2  # type: ignore
        self.cv2 = cv2
        self.sift = cv2.SIFT_create(nfeatures=3000)
        self.matcher = cv2.BFMatcher(cv2.NORM_L2)
        self.references: list[dict[str, Any]] = []
        self.calibration = calibration
        self.camera_state = TrackingState()
        self.probe_state = TrackingState()
        self.camera_min_inliers = CAMERA_MIN_INLIERS
        scale = float((similarity or {}).get("scale", 1.0))
        rotation = np.asarray((similarity or {}).get("rotation", np.eye(3).reshape(-1)), dtype=float).reshape(3, 3)
        translation = np.asarray((similarity or {}).get("translation", [0, 0, 0]), dtype=float)
        for frame_index, frame in enumerate(references[:12]):
            width, height = int(frame["width"]), int(frame["height"])
            rgb = np.fromfile(artifact_root / frame["rgb_artifact"]["relative_uri"], dtype=np.uint8).reshape(height, width, 3)
            depth = np.fromfile(artifact_root / frame["depth_artifact"]["relative_uri"], dtype="<f4").reshape(height, width)
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            keypoints, descriptors = self.sift.detectAndCompute(gray, None)
            if descriptors is None:
                continue
            k = np.asarray(frame["intrinsic_matrix"], dtype=float).reshape(3, 3)
            world_points = np.full((len(keypoints), 3), np.nan, dtype=np.float32)
            for index, keypoint in enumerate(keypoints):
                u, v = int(round(keypoint.pt[0])), int(round(keypoint.pt[1]))
                if not (0 <= u < width and 0 <= v < height):
                    continue
                z = float(depth[v, u])
                if not math.isfinite(z) or not 0.05 < z < 5:
                    continue
                point_m0 = np.asarray([(u - k[0, 2]) * z / k[0, 0] + frame_index * 0.0025, (v - k[1, 2]) * z / k[1, 1], z])
                world_points[index] = scale * (rotation @ point_m0) + translation
            self.references.append({"descriptors": descriptors, "world_points": world_points})

    def _localize(self, frame: NormalizedCameraFrame) -> tuple[np.ndarray | None, int, float, str | None]:
        cv2 = self.cv2
        rgb = np.frombuffer(frame.rgb, dtype=np.uint8).reshape(frame.height, frame.width, 3)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        current_keypoints, current_descriptors = self.sift.detectAndCompute(gray, None)
        if current_descriptors is None:
            return None, 0, math.inf, "no_current_descriptors"
        best: tuple[np.ndarray | None, int, float, str | None] = (None, 0, math.inf, "insufficient_reference_matches")
        k = np.asarray(frame.intrinsic_matrix, dtype=float).reshape(3, 3)
        for reference in self.references:
            matches = self.matcher.knnMatch(reference["descriptors"], current_descriptors, k=2)
            good = [first for first, second in matches if first.distance < 0.75 * second.distance and np.isfinite(reference["world_points"][first.queryIdx]).all()]
            if len(good) < 6:
                continue
            object_points = np.asarray([reference["world_points"][item.queryIdx] for item in good], dtype=np.float32)
            image_points = np.asarray([current_keypoints[item.trainIdx].pt for item in good], dtype=np.float32)
            success, rvec, tvec, inliers = cv2.solvePnPRansac(object_points, image_points, k, None, iterationsCount=200, reprojectionError=3.0, confidence=0.999, flags=cv2.SOLVEPNP_EPNP)
            inlier_count = 0 if not success or inliers is None else len(inliers)
            if not success or inlier_count < 6:
                continue
            projected, _ = cv2.projectPoints(object_points[inliers[:, 0]], rvec, tvec, k, None)
            error = float(np.sqrt(np.mean(np.sum((projected[:, 0] - image_points[inliers[:, 0]]) ** 2, axis=1))))
            rotation_c_w, _ = cv2.Rodrigues(rvec)
            t_c_w = np.eye(4); t_c_w[:3, :3] = rotation_c_w; t_c_w[:3, 3] = tvec[:, 0]
            t_w_c = np.linalg.inv(t_c_w)
            if inlier_count > best[1] or (inlier_count == best[1] and error < best[2]):
                best = (t_w_c, inlier_count, error, None)
        return best

    def _probe_pose(self, frame: NormalizedCameraFrame) -> tuple[np.ndarray | None, int, float, str | None, int]:
        cv2 = self.cv2
        marker_points = np.asarray(self.calibration["probe"]["marker_points_m"], dtype=np.float32)
        result = detect_blobs(frame.rgb, frame.width, frame.height, self.calibration["blob_detector"], intrinsic_matrix=frame.intrinsic_matrix, marker_points_m=marker_points)
        points = result.get("keypoints", [])
        candidate_count = len(points)
        if candidate_count < 5:
            return None, 0, math.inf, "fewer_than_five_blobs", candidate_count
        candidates = np.asarray([[item["x"], item["y"]] for item in points[:6]], dtype=np.float32)
        k = np.asarray(frame.intrinsic_matrix, dtype=float).reshape(3, 3)
        best: tuple[np.ndarray | None, int, float, str | None, int] = (None, 0, math.inf, "probe_correspondence_failed", candidate_count)
        for selection in itertools.combinations(range(len(candidates)), 5):
            selected = candidates[list(selection)]
            for permutation in itertools.permutations(range(5)):
                image_points = selected[list(permutation)]
                success, rvec, tvec, inliers = cv2.solvePnPRansac(marker_points, image_points, k, None, iterationsCount=60, reprojectionError=2.5, confidence=0.995, flags=cv2.SOLVEPNP_EPNP)
                count = 0 if not success or inliers is None else len(inliers)
                if not success or count < PROBE_MIN_INLIERS or float(tvec[2, 0]) <= 0:
                    continue
                success, rvec, tvec = cv2.solvePnP(marker_points[inliers[:, 0]], image_points[inliers[:, 0]], k, None, rvec, tvec, True, flags=cv2.SOLVEPNP_ITERATIVE)
                projected, _ = cv2.projectPoints(marker_points, rvec, tvec, k, None)
                error = float(np.sqrt(np.mean(np.sum((projected[:, 0] - image_points) ** 2, axis=1))))
                rotation_c_m, _ = cv2.Rodrigues(rvec)
                pose = np.eye(4); pose[:3, :3] = rotation_c_m; pose[:3, 3] = tvec[:, 0]
                if count > best[1] or (count == best[1] and error < best[2]):
                    best = (pose, count, error, None, candidate_count)
        return best

    def track(self, session_id: str, frame: NormalizedCameraFrame) -> dict[str, Any]:
        started = time.monotonic_ns()
        raw_w_c, camera_inliers, camera_error, camera_reason = self._localize(frame)
        raw_c_m, probe_inliers, probe_error, probe_reason, blob_count = self._probe_pose(frame)
        camera_acceptable = raw_w_c is not None and camera_inliers >= self.camera_min_inliers and camera_error <= CAMERA_MAX_REPROJECTION_ERROR_PX
        probe_acceptable = raw_c_m is not None and probe_inliers >= PROBE_MIN_INLIERS and probe_error <= PROBE_MAX_REPROJECTION_ERROR_PX
        camera_state, t_w_c, camera_temporal = self.camera_state.update(raw_w_c if camera_acceptable else None)
        probe_state, t_c_m, probe_temporal = self.probe_state.update(raw_c_m if probe_acceptable else None)
        latency_ms = (time.monotonic_ns() - started) / 1e6
        poses_valid = camera_state == probe_state == "tracked" and t_w_c is not None and t_c_m is not None
        latency_ok = latency_ms <= MAX_TRACKING_LATENCY_MS
        # Compute tip whenever both poses exist — don't suppress the tip on latency alone
        t_w_p = compose_tip(t_w_c.reshape(-1).tolist(), t_c_m.reshape(-1).tolist(), self.calibration["probe"]["t_marker_tip"]) if poses_valid else None
        tip = np.asarray(t_w_p).reshape(4, 4)[:3, 3].tolist() if t_w_p else None
        # quality == "good" only when poses are valid AND latency is within threshold
        tracked = poses_valid and latency_ok
        return {
            "session_id": session_id, "frame_id": frame.sequence, "device_timestamp_ns": frame.device_timestamp_ns,
            "camera_state": "tracked" if camera_state == "tracked" else "lost", "probe_state": "tracked" if probe_state == "tracked" else "lost",
            "t_w_c": t_w_c.reshape(-1).tolist() if t_w_c is not None else None, "t_c_m": t_c_m.reshape(-1).tolist() if t_c_m is not None else None,
            "t_w_p": t_w_p, "tip_w_m": tip, "camera_inliers": camera_inliers, "camera_reprojection_error_px": camera_error if math.isfinite(camera_error) else None,
            "probe_inliers": probe_inliers, "probe_reprojection_error_px": probe_error if math.isfinite(probe_error) else None,
            "blob_count": blob_count, "fps": 0.0, "latency_ms": latency_ms, "quality": "good" if tracked else "lost",
            "rejection_reasons": [item for item in (camera_reason, camera_temporal, probe_reason, probe_temporal, "latency" if not latency_ok else None) if item],
            "coordinate_frame": "W", "units": "m", "simulated": False,
        }
