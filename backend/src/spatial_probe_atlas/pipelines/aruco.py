from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np

ZERO_DISTORTION = np.zeros((5, 1), dtype=np.float64)


@dataclass
class PoseEstimate:
    rvec: np.ndarray
    tvec: np.ndarray
    reprojection_error_px: float
    depth_error_m: float
    matched_indices: tuple[int, ...] = ()


def marker_object_points(marker_size_m: float) -> np.ndarray:
    """Corners in ArUco canonical order: TL, TR, BR, BL."""
    half = marker_size_m / 2.0
    return np.array(
        [[-half, half, 0.0], [half, half, 0.0], [half, -half, 0.0], [-half, -half, 0.0]],
        dtype=np.float64,
    )


def matrix_from_pose(rvec: np.ndarray, tvec: np.ndarray) -> np.ndarray:
    import cv2  # type: ignore

    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = cv2.Rodrigues(np.asarray(rvec, dtype=np.float64))[0]
    result[:3, 3] = np.asarray(tvec, dtype=np.float64).reshape(3)
    return result


def transform_points(transform: np.ndarray, points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    return (transform[:3, :3] @ points.T).T + transform[:3, 3]


def reprojection_error(
    object_points: np.ndarray,
    image_points: np.ndarray,
    rvec: np.ndarray,
    tvec: np.ndarray,
    K: np.ndarray,
) -> float:
    import cv2  # type: ignore

    projected = cv2.projectPoints(object_points, rvec, tvec, K, ZERO_DISTORTION)[0].reshape(-1, 2)
    return float(np.sqrt(np.mean(np.sum((projected - np.asarray(image_points).reshape(-1, 2)) ** 2, axis=1))))


def estimate_planar_pose(
    object_points: np.ndarray,
    image_points: np.ndarray,
    K: np.ndarray,
) -> PoseEstimate | None:
    import cv2  # type: ignore

    object_points = np.asarray(object_points, dtype=np.float64).reshape(-1, 3)
    image_points = np.asarray(image_points, dtype=np.float64).reshape(-1, 2)
    if len(object_points) < 4:
        return None
    
    rvecs, tvecs = [], []
    try:
        solved = cv2.solvePnPGeneric(
            object_points,
            image_points,
            K,
            ZERO_DISTORTION,
            flags=cv2.SOLVEPNP_IPPE,
        )
        if solved[0] and len(solved[1]) > 0:
            rvecs, tvecs = list(solved[1]), list(solved[2])
    except Exception:
        pass

    if not rvecs:
        try:
            flag = cv2.SOLVEPNP_SQPNP if hasattr(cv2, "SOLVEPNP_SQPNP") else cv2.SOLVEPNP_ITERATIVE
            ok, rvec, tvec = cv2.solvePnP(
                object_points,
                image_points,
                K,
                ZERO_DISTORTION,
                flags=flag,
            )
            if ok:
                rvecs, tvecs = [rvec], [tvec]
        except Exception:
            pass

    if not rvecs:
        return None

    candidates: list[tuple[float, PoseEstimate]] = []
    for rvec, tvec in zip(rvecs, tvecs):
        camera_points = transform_points(matrix_from_pose(rvec, tvec), object_points)
        if np.any(camera_points[:, 2] <= 0.0):
            continue
        pixel_error = reprojection_error(object_points, image_points, rvec, tvec, K)
        candidates.append((pixel_error, PoseEstimate(rvec.copy(), tvec.copy(), pixel_error, 0.0)))

    if not candidates:
        return None
    result = min(candidates, key=lambda item: item[0])[1]
    try:
        result.rvec, result.tvec = cv2.solvePnPRefineLM(
            object_points,
            image_points,
            K,
            ZERO_DISTORTION,
            result.rvec,
            result.tvec,
        )
        result.reprojection_error_px = reprojection_error(
            object_points, image_points, result.rvec, result.tvec, K
        )
    except Exception:
        pass
    return result


def estimate_board_pose(
    marker_layout: dict[int, np.ndarray],
    detections: dict[int, np.ndarray],
    K: np.ndarray,
    minimum_markers: int = 1,
) -> tuple[PoseEstimate | None, list[int]]:
    used_ids = sorted(set(marker_layout).intersection(detections))
    if len(used_ids) < minimum_markers:
        return None, used_ids
    object_points = np.concatenate([marker_layout[marker_id] for marker_id in used_ids], axis=0)
    image_points = np.concatenate([detections[marker_id] for marker_id in used_ids], axis=0)
    return (
        estimate_planar_pose(object_points, image_points, K),
        used_ids,
    )


def ray_intersection_with_object_plane(
    image_point: np.ndarray,
    K: np.ndarray,
    object_to_camera: np.ndarray,
) -> np.ndarray | None:
    ray = np.linalg.inv(K) @ np.array([image_point[0], image_point[1], 1.0], dtype=np.float64)
    rotation = object_to_camera[:3, :3]
    translation = object_to_camera[:3, 3]
    system = np.column_stack((rotation[:, 0], rotation[:, 1], -ray))
    try:
        x, y, ray_scale = np.linalg.solve(system, -translation)
    except np.linalg.LinAlgError:
        return None
    if ray_scale <= 0.0:
        return None
    return np.array([x, y, 0.0], dtype=np.float64)


def capture_board_observation(
    marker_ids: list[int],
    anchor_id: int,
    marker_size_m: float,
    detections: dict[int, np.ndarray],
    K: np.ndarray,
) -> tuple[dict[int, np.ndarray], str]:
    if anchor_id not in detections:
        return {}, "anchor marker is not visible"
    anchor_points = marker_object_points(marker_size_m)
    anchor_pose = estimate_planar_pose(anchor_points, detections[anchor_id], K)
    if anchor_pose is None:
        return {}, "anchor pose failed its PnP check"
    anchor_to_camera = matrix_from_pose(anchor_pose.rvec, anchor_pose.tvec)
    observation: dict[int, np.ndarray] = {anchor_id: anchor_points}

    for marker_id in marker_ids:
        if marker_id == anchor_id or marker_id not in detections:
            continue
        corners_3d = [
            ray_intersection_with_object_plane(point, K, anchor_to_camera)
            for point in detections[marker_id]
        ]
        if any(point is None for point in corners_3d):
            continue
        observation[marker_id] = np.asarray(corners_3d, dtype=np.float64)

    if len(observation) == 1:
        return {}, "no target marker passed the plane check"
    return observation, "ok"


def fit_metric_square(raw_corners: np.ndarray, marker_size_m: float) -> tuple[np.ndarray, float]:
    """Fit a fixed-size 2-D square while retaining decoded corner order."""
    canonical = marker_object_points(marker_size_m)[:, :2]
    target = np.asarray(raw_corners, dtype=np.float64)[:, :2]
    target_centre = np.mean(target, axis=0)
    source = canonical - np.mean(canonical, axis=0)
    centred_target = target - target_centre
    u, _, vt = np.linalg.svd(source.T @ centred_target)
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        u[:, -1] *= -1.0
        rotation = u @ vt
    fitted_xy = source @ rotation + target_centre
    residual = float(np.sqrt(np.mean(np.sum((fitted_xy - target) ** 2, axis=1))))
    return np.column_stack((fitted_xy, np.zeros(4))), residual


def finalize_board_layout(
    marker_ids: list[int],
    anchor_id: int,
    marker_size_m: float,
    samples: dict[int, list[np.ndarray]],
    minimum_samples: int,
) -> tuple[dict[int, np.ndarray] | None, dict[str, object]]:
    missing = [marker_id for marker_id in marker_ids if len(samples[marker_id]) < minimum_samples]
    if missing:
        return None, {"error": f"need {minimum_samples} samples for marker(s) {missing}"}

    layout_anchor: dict[int, np.ndarray] = {anchor_id: marker_object_points(marker_size_m)}
    fit_residuals: dict[str, float] = {str(anchor_id): 0.0}
    sample_spread: dict[str, float] = {str(anchor_id): 0.0}
    for marker_id in marker_ids:
        if marker_id == anchor_id:
            continue
        stack = np.stack(samples[marker_id], axis=0)
        median_corners = np.median(stack, axis=0)
        fitted, fit_residual = fit_metric_square(median_corners, marker_size_m)
        layout_anchor[marker_id] = fitted
        fit_residuals[str(marker_id)] = fit_residual
        sample_spread[str(marker_id)] = float(
            np.sqrt(np.mean((stack - median_corners[None, :, :]) ** 2))
        )

    # Equal-size markers: mean marker centre is an intuitive board origin.
    board_origin_in_anchor = np.mean(
        np.stack([np.mean(layout_anchor[marker_id], axis=0) for marker_id in marker_ids]),
        axis=0,
    )
    layout_board = {
        marker_id: corners - board_origin_in_anchor for marker_id, corners in layout_anchor.items()
    }
    diagnostics: dict[str, object] = {
        "sample_counts": {str(marker_id): len(samples[marker_id]) for marker_id in marker_ids},
        "square_fit_rms_m": fit_residuals,
        "sample_spread_rms_m": sample_spread,
        "board_origin_in_anchor_m": board_origin_in_anchor.tolist(),
    }
    return layout_board, diagnostics


def optimize_joint(
    nominal_layout: dict[int, np.ndarray],
    frames: list[dict[str, Any]],
    anchor_id: int,
) -> np.ndarray | None:
    import scipy.optimize  # type: ignore

    def residuals(params: np.ndarray) -> np.ndarray:
        alpha = params[0]
        t_tip = params[1:4]
        scaled_layout = {k: v * alpha for k, v in nominal_layout.items()}
        res = []
        for frame in frames:
            probe_rvec = np.asarray(frame["probe_pose"]["rvec"])
            probe_tvec = np.asarray(frame["probe_pose"]["tvec"])

            K = np.asarray(frame["K"])
            detections = {int(k): np.asarray(v) for k, v in frame["detections"].items()}
            board_pose, _ = estimate_board_pose(
                scaled_layout, detections, K,
                minimum_markers=1
            )
            if board_pose is None:
                res.extend([1000.0, 1000.0, 1000.0])
                continue

            target_board = np.mean(scaled_layout[anchor_id], axis=0).reshape(1, 3)
            board_to_cam = matrix_from_pose(board_pose.rvec, board_pose.tvec)
            target_cam = transform_points(board_to_cam, target_board)[0]

            probe_to_cam = matrix_from_pose(probe_rvec, probe_tvec)
            tip_cam = transform_points(probe_to_cam, t_tip.reshape(1, 3))[0]

            res.extend(target_cam - tip_cam)
        return np.array(res, dtype=np.float64)

    x0 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float64)
    result = scipy.optimize.least_squares(residuals, x0, method='lm')
    return result.x


_ARUCO_DETECTOR_CACHE: dict[str, Any] = {}

def get_aruco_detector(cv2: Any, dictionary_name: str = "DICT_4X4_50") -> Any | None:
    if not hasattr(cv2.aruco, dictionary_name):
        return None
    if dictionary_name not in _ARUCO_DETECTOR_CACHE:
        dictionary_id = int(getattr(cv2.aruco, dictionary_name))
        dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
        params = cv2.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        _ARUCO_DETECTOR_CACHE[dictionary_name] = cv2.aruco.ArucoDetector(dictionary, params)
    return _ARUCO_DETECTOR_CACHE[dictionary_name]


def detect_aruco(
    rgb: bytes | np.ndarray,
    width: int,
    height: int,
    dictionary_name: str = "DICT_4X4_50",
    expected_ids: Iterable[int] | None = None,
    gray_image: np.ndarray | None = None,
    detector: Any | None = None,
) -> tuple[dict[int, list[list[float]]], Any]:
    try:
        import cv2  # type: ignore
    except Exception:
        return {}, None

    if detector is None:
        detector = get_aruco_detector(cv2, dictionary_name)
    if detector is None:
        return {}, None

    if gray_image is not None:
        gray = gray_image
    elif isinstance(rgb, np.ndarray):
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY) if rgb.ndim == 3 else rgb
    else:
        image = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

    corners, ids, rejected = detector.detectMarkers(gray)

    detections = {}
    if ids is not None:
        expected_set = set(expected_ids) if expected_ids else None
        for i, marker_id_raw in enumerate(ids.flat):
            m_id = int(marker_id_raw)
            if expected_set is None or m_id in expected_set:
                # corners[i] is 1x4x2, convert to list of [x, y]
                detections[m_id] = corners[i][0].tolist()

    return detections, (corners, ids)
