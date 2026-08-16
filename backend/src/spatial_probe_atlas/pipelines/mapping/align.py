from __future__ import annotations

"""Robust alignment of an SfM map to a rigid ArUco board.

The board is deliberately kept rigid. We optimise a single seven degree of
freedom similarity transform from the SfM map frame (M0) into the board frame
(W), using every detected marker corner from every registered SfM camera. A
per-view PnP result is only used to seed and robustly screen the optimisation;
it is not the final measurement.
"""

from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np

from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.domain.transforms import solve_similarity
from spatial_probe_atlas.pipelines.aruco import (
    estimate_board_pose,
    detect_aruco,
    marker_object_points,
    estimate_planar_pose,
)


MIN_ALIGNMENT_VIEWS = 3
MIN_ALIGNMENT_CORNERS = 12
MAX_CORNER_INLIER_ERROR_PX = 3.0
MAX_RANSAC_ITERATIONS = 512


@dataclass(frozen=True)
class AlignmentView:
    """Corners observed by one registered SfM camera.

    ``camera_rotation`` and ``camera_translation`` represent ``T_C_M0``.
    ``board_camera_center`` is the PnP estimate of the same camera centre in
    the rigid board frame and is used only for a robust initial similarity.
    """

    view_id: str
    camera_rotation: np.ndarray
    camera_translation: np.ndarray
    intrinsics: np.ndarray
    board_points: np.ndarray
    image_points: np.ndarray
    map_camera_center: np.ndarray
    board_camera_center: np.ndarray
    marker_ids: tuple[int, ...]


def _as_rotation_matrix(value: Any) -> np.ndarray:
    matrix = value.matrix() if callable(getattr(value, "matrix", None)) else value.matrix
    result = np.asarray(matrix, dtype=np.float64).reshape(3, 3)
    if not np.isfinite(result).all() or not np.allclose(result.T @ result, np.eye(3), atol=1e-4):
        raise ValueError("SfM camera rotation is not a finite rigid rotation")
    return result


def _camera_pose(image: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return R_C_M0, t_C_M0, and the camera centre C_M0 for pycolmap APIs."""
    cam_from_world = image.cam_from_world() if callable(getattr(image, "cam_from_world", None)) else image.cam_from_world
    rotation = cam_from_world.rotation() if callable(getattr(cam_from_world, "rotation", None)) else cam_from_world.rotation
    translation = cam_from_world.translation() if callable(getattr(cam_from_world, "translation", None)) else cam_from_world.translation
    r_c_m0 = _as_rotation_matrix(rotation)
    t_c_m0 = np.asarray(translation, dtype=np.float64).reshape(3)
    if not np.isfinite(t_c_m0).all():
        raise ValueError("SfM camera translation is not finite")
    return r_c_m0, t_c_m0, -r_c_m0.T @ t_c_m0


def _frame_for_image(image_name: str, frames_metadata: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Map the CUDA mapper's ``frame-000000.png`` name back to a capture frame."""
    stem = Path(image_name).stem
    try:
        index = int(stem.rsplit("-", 1)[-1])
    except ValueError:
        return None
    if 0 <= index < len(frames_metadata):
        return frames_metadata[index]
    return next((frame for frame in frames_metadata if int(frame.get("sequence", -1)) == index), None)


def _read_rgb(artifact_root: Path, frame: dict[str, Any]) -> np.ndarray | None:
    import cv2  # type: ignore

    artifact = frame.get("rgb_artifact") or {}
    relative_uri = artifact.get("relative_uri")
    if not relative_uri:
        return None
    path = artifact_root / relative_uri
    if not path.is_file():
        return None
    width, height = int(frame["width"]), int(frame["height"])
    if path.suffix == ".rgb8":
        values = np.fromfile(path, dtype=np.uint8)
        if values.size != width * height * 3:
            return None
        return values.reshape(height, width, 3)
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB) if image is not None else None


def _candidate_similarity(source: np.ndarray, target: np.ndarray) -> tuple[dict[str, Any], np.ndarray] | None:
    """Fit many three-view hypotheses and retain the best metric inlier set."""
    if len(source) < MIN_ALIGNMENT_VIEWS:
        return None
    # ArUco PnP centres should agree to millimetres. This modestly permissive
    # limit rejects a bad marker pose before it can bias the corner refinement.
    inlier_threshold_m = 0.015
    candidates = list(combinations(range(len(source)), MIN_ALIGNMENT_VIEWS))
    if len(candidates) > MAX_RANSAC_ITERATIONS:
        rng = np.random.default_rng(0)
        chosen = rng.choice(len(candidates), size=MAX_RANSAC_ITERATIONS, replace=False)
        candidates = [candidates[int(index)] for index in chosen]

    best: tuple[int, float, dict[str, Any], np.ndarray] | None = None
    for indices in candidates:
        try:
            solution = solve_similarity(source[list(indices)].tolist(), target[list(indices)].tolist())
        except AppError:
            continue
        rotation = np.asarray(solution["rotation"], dtype=np.float64).reshape(3, 3)
        transformed = (float(solution["scale"]) * (rotation @ source.T)).T + np.asarray(solution["translation"], dtype=np.float64)
        residuals = np.linalg.norm(transformed - target, axis=1)
        inliers = residuals <= inlier_threshold_m
        score = (int(inliers.sum()), -float(np.mean(residuals[inliers] ** 2)) if inliers.any() else float("-inf"))
        if best is None or score > best[:2]:
            best = (score[0], score[1], solution, inliers)

    if best is None or best[0] < MIN_ALIGNMENT_VIEWS:
        return None
    try:
        return solve_similarity(source[best[3]].tolist(), target[best[3]].tolist()), best[3]
    except AppError:
        return None


def _similarity_parameters(solution: dict[str, Any]) -> np.ndarray:
    from scipy.spatial.transform import Rotation

    rotation = Rotation.from_matrix(np.asarray(solution["rotation"], dtype=np.float64).reshape(3, 3)).as_rotvec()
    return np.concatenate((rotation, np.asarray(solution["translation"], dtype=np.float64).reshape(3), [np.log(float(solution["scale"]))]))


def _corner_residuals(parameters: np.ndarray, views: list[AlignmentView]) -> np.ndarray:
    """Reproject W-frame board corners through T_C_M0 and T_W_M0."""
    from scipy.spatial.transform import Rotation

    r_w_m0 = Rotation.from_rotvec(parameters[:3]).as_matrix()
    t_w_m0 = parameters[3:6]
    scale = float(np.exp(parameters[6]))
    residuals: list[np.ndarray] = []
    for view in views:
        # p_w = s R_W_M0 p_m0 + t. Transform known board points back into
        # the SfM map, then use the fixed SfM camera pose to predict pixels.
        points_m0 = ((view.board_points - t_w_m0) @ r_w_m0) / scale
        points_c = points_m0 @ view.camera_rotation.T + view.camera_translation
        homogeneous = points_c @ view.intrinsics.T
        depth = homogeneous[:, 2]
        pixels = np.empty((len(points_c), 2), dtype=np.float64)
        valid = depth > 1e-8
        pixels[valid] = homogeneous[valid, :2] / depth[valid, None]
        pixels[~valid] = view.image_points[~valid] + 1e4
        residuals.append(pixels - view.image_points)
    return np.concatenate(residuals, axis=0)


def _corner_view_indices(views: list[AlignmentView]) -> np.ndarray:
    return np.concatenate([np.full(len(view.board_points), index, dtype=int) for index, view in enumerate(views)])


def _subset_views_by_corners(views: list[AlignmentView], corner_mask: np.ndarray) -> list[AlignmentView]:
    result: list[AlignmentView] = []
    start = 0
    for view in views:
        stop = start + len(view.board_points)
        selected = corner_mask[start:stop]
        if selected.any():
            result.append(AlignmentView(
                view_id=view.view_id, camera_rotation=view.camera_rotation,
                camera_translation=view.camera_translation, intrinsics=view.intrinsics,
                board_points=view.board_points[selected], image_points=view.image_points[selected],
                map_camera_center=view.map_camera_center, board_camera_center=view.board_camera_center,
                marker_ids=view.marker_ids,
            ))
        start = stop
    return result


def refine_similarity_from_views(views: list[AlignmentView]) -> dict[str, Any]:
    """Robustly estimate T_W_M0 from every valid board-corner observation.

    The first stage RANSACs per-view PnP camera centres. The second stage
    minimises the actual 2D corner reprojection residuals with a Huber loss,
    discards remaining bad corners, and refines only the resulting inliers.
    """
    from scipy.optimize import least_squares
    from scipy.spatial.transform import Rotation

    if len(views) < MIN_ALIGNMENT_VIEWS:
        raise AppError("ALIGNMENT_VIEWS_INSUFFICIENT", f"At least {MIN_ALIGNMENT_VIEWS} valid views are required for alignment, but only got {len(views)}.", status_code=422)
    centres_m0 = np.asarray([view.map_camera_center for view in views], dtype=np.float64)
    centres_w = np.asarray([view.board_camera_center for view in views], dtype=np.float64)
    seeded = _candidate_similarity(centres_m0, centres_w)
    if seeded is None:
        raise AppError("ALIGNMENT_INITIALIZATION_FAILED", "The ArUco camera-pose observations could not establish a non-degenerate initial map alignment.", status_code=422, suggested_action="Capture the board from at least three distinct positions and angles.")
    initial, view_inliers = seeded
    candidate_views = [view for view, is_inlier in zip(views, view_inliers) if is_inlier]
    if len(candidate_views) < MIN_ALIGNMENT_VIEWS:
        raise AppError("ALIGNMENT_VIEWS_INSUFFICIENT", "Too few ArUco camera-pose observations remained after robust screening.", status_code=422)

    first = least_squares(lambda parameters: _corner_residuals(parameters, candidate_views).ravel(), _similarity_parameters(initial), loss="huber", f_scale=2.0, max_nfev=500)
    corner_errors = np.linalg.norm(_corner_residuals(first.x, candidate_views), axis=1)
    corner_inliers = corner_errors <= MAX_CORNER_INLIER_ERROR_PX
    view_indices = _corner_view_indices(candidate_views)
    if int(corner_inliers.sum()) < MIN_ALIGNMENT_CORNERS or len(np.unique(view_indices[corner_inliers])) < MIN_ALIGNMENT_VIEWS:
        raise AppError(
            "ALIGNMENT_CORNERS_INSUFFICIENT",
            f"At least {MIN_ALIGNMENT_CORNERS} corners across {MIN_ALIGNMENT_VIEWS} views must agree within {MAX_CORNER_INLIER_ERROR_PX:g} px.",
            status_code=422,
            suggested_action="Recapture sharp, well-lit views with a larger range of camera positions.",
            details={"corner_inliers": int(corner_inliers.sum()), "corner_observations": int(len(corner_errors))},
        )
    inlier_views = _subset_views_by_corners(candidate_views, corner_inliers)
    final = least_squares(lambda parameters: _corner_residuals(parameters, inlier_views).ravel(), first.x, loss="linear", max_nfev=500)
    final_errors = np.linalg.norm(_corner_residuals(final.x, inlier_views), axis=1)
    r_w_m0 = Rotation.from_rotvec(final.x[:3]).as_matrix()
    scale = float(np.exp(final.x[6]))
    translation = final.x[3:6]
    centres_m0_used = np.asarray([view.map_camera_center for view in inlier_views], dtype=np.float64)
    centres_w_used = np.asarray([view.board_camera_center for view in inlier_views], dtype=np.float64)
    centre_errors = np.linalg.norm((scale * (r_w_m0 @ centres_m0_used.T)).T + translation - centres_w_used, axis=1)

    return {
        "source_frame": "M0", "destination_frame": "W", "units": "m", "convention_version": 2,
        "method": "robust_corner_reprojection_v1", "scale": scale,
        "rotation": r_w_m0.reshape(-1).tolist(), "translation": translation.tolist(),
        "rms_residual_m": float(np.sqrt(np.mean(centre_errors**2))), "max_residual_m": float(centre_errors.max()),
        "residuals_m": centre_errors.tolist(), "observation_count": int(len(inlier_views)),
        "view_count": int(len(views)), "inlier_view_count": int(len(inlier_views)),
        "corner_observation_count": int(sum(len(view.board_points) for view in candidate_views)),
        "corner_inlier_count": int(len(final_errors)),
        "rms_reprojection_error_px": float(np.sqrt(np.mean(final_errors**2))),
        "max_reprojection_error_px": float(final_errors.max()),
        "p95_reprojection_error_px": float(np.percentile(final_errors, 95)),
        "marker_ids": sorted({marker_id for view in inlier_views for marker_id in view.marker_ids}),
    }


def align_map_to_aruco(
    artifact_root: Path,
    sfm_dir: Path,
    frames_metadata: list[dict[str, Any]],
    marker_ids: list[int],
    board_layout: dict[int, np.ndarray],
    capture_dir: Path | None = None,
) -> dict[str, Any]:
    """Align a CUDA SfM map to a board using every visible requested marker."""
    import pycolmap

    rec_dir = sfm_dir
    if not (rec_dir / "cameras.bin").exists() and not (rec_dir / "cameras.txt").exists():
        candidates = list(sfm_dir.glob("**/cameras.bin")) + list(sfm_dir.glob("**/cameras.txt"))
        if candidates:
            rec_dir = candidates[0].parent
    try:
        model = pycolmap.Reconstruction(str(rec_dir))
    except Exception as exc:
        raise AppError("SFM_MODEL_INVALID", f"Could not load pycolmap Reconstruction from {rec_dir}: {exc}", status_code=500) from exc

    requested_ids = [int(marker_id) for marker_id in marker_ids]
    missing_layout = [marker_id for marker_id in requested_ids if marker_id not in board_layout]
    if missing_layout:
        raise AppError("ARUCO_BOARD_LAYOUT_INCOMPLETE", "The selected marker IDs are missing from the virtual board layout.", status_code=422, details={"missing_marker_ids": missing_layout})

    views: list[AlignmentView] = []
    for image_id, image in sorted(model.images.items()):
        frame = _frame_for_image(image.name, frames_metadata)
        if frame is None:
            continue
        rgb = _read_rgb(artifact_root, frame)
        if rgb is None:
            continue
        try:
            r_c_m0, t_c_m0, c_m0 = _camera_pose(image)
            camera = model.cameras[image.camera_id]
            k_matrix = np.asarray(camera.calibration_matrix(), dtype=np.float64).reshape(3, 3)
        except Exception:
            continue
        detections, _ = detect_aruco(rgb, int(frame["width"]), int(frame["height"]), "DICT_4X4_50", requested_ids)
        used_ids = tuple(sorted(set(detections).intersection(requested_ids)))
        if not used_ids:
            continue
        board_points = np.concatenate([np.asarray(board_layout[marker_id], dtype=np.float64).reshape(4, 3) for marker_id in used_ids])
        image_points = np.concatenate([np.asarray(detections[marker_id], dtype=np.float64).reshape(4, 2) for marker_id in used_ids])
        pose, _ = estimate_board_pose(board_layout, detections, k_matrix, minimum_markers=1)
        if pose is None:
            continue
        import cv2  # type: ignore

        r_c_w = cv2.Rodrigues(pose.rvec)[0]
        c_w = -r_c_w.T @ np.asarray(pose.tvec, dtype=np.float64).reshape(3)
        views.append(AlignmentView(
            view_id=str(image_id), camera_rotation=r_c_m0, camera_translation=t_c_m0,
            intrinsics=k_matrix, board_points=board_points, image_points=image_points,
            map_camera_center=c_m0, board_camera_center=c_w, marker_ids=used_ids,
        ))
    return refine_similarity_from_views(views)


def extract_scene_markers(
    artifact_root: Path,
    sfm_dir: Path,
    frames_metadata: list[dict[str, Any]],
    nominal_marker_size_m: float = 0.035,
    dictionary_name: str = "DICT_4X4_50",
) -> list[dict[str, Any]]:
    """Scan registered SfM views, detect all ArUco markers, and triangulate their 3D corners in M0."""
    import pycolmap

    rec_dir = sfm_dir
    if not (rec_dir / "cameras.bin").exists() and not (rec_dir / "cameras.txt").exists():
        candidates = list(sfm_dir.glob("**/cameras.bin")) + list(sfm_dir.glob("**/cameras.txt"))
        if candidates:
            rec_dir = candidates[0].parent
    if not (rec_dir / "cameras.bin").exists() and not (rec_dir / "cameras.txt").exists():
        return []

    try:
        model = pycolmap.Reconstruction(str(rec_dir))
    except Exception:
        return []

    # Map marker_id -> list of (camera_center_m0, list_of_4_unit_rays_m0)
    marker_rays: dict[int, list[tuple[np.ndarray, list[np.ndarray]]]] = {}

    for _, image in sorted(model.images.items()):
        frame = _frame_for_image(image.name, frames_metadata)
        if frame is None:
            continue
        rgb = _read_rgb(artifact_root, frame)
        if rgb is None:
            continue
        try:
            r_c_m0, t_c_m0, c_m0 = _camera_pose(image)
            camera = model.cameras[image.camera_id]
            k_matrix = np.asarray(camera.calibration_matrix(), dtype=np.float64).reshape(3, 3)
            k_inv = np.linalg.inv(k_matrix)
        except Exception:
            continue

        detections, _ = detect_aruco(rgb, int(frame["width"]), int(frame["height"]), dictionary_name)
        if not detections:
            continue

        for marker_id, img_corners in detections.items():
            corners_arr = np.asarray(img_corners, dtype=np.float64).reshape(4, 2)
            rays_4 = []
            for j in range(4):
                px = np.array([corners_arr[j, 0], corners_arr[j, 1], 1.0], dtype=np.float64)
                ray_c = k_inv @ px
                ray_m0 = r_c_m0.T @ ray_c
                norm = np.linalg.norm(ray_m0)
                if norm > 1e-8:
                    ray_m0 /= norm
                rays_4.append(ray_m0)
            marker_rays.setdefault(int(marker_id), []).append((c_m0, rays_4))

    results: list[dict[str, Any]] = []
    for marker_id in sorted(marker_rays.keys()):
        obs = marker_rays[marker_id]
        if not obs:
            continue

        corners_3d = []
        for j in range(4):
            if len(obs) >= 2:
                # Solve least-squares ray intersection: sum (I - v v^T) X = sum (I - v v^T) o
                a_mat = np.zeros((3, 3), dtype=np.float64)
                b_vec = np.zeros(3, dtype=np.float64)
                for o, rays in obs:
                    v = rays[j]
                    proj = np.eye(3) - np.outer(v, v)
                    a_mat += proj
                    b_vec += proj @ o
                try:
                    pt_3d = np.linalg.lstsq(a_mat, b_vec, rcond=None)[0]
                except Exception:
                    pt_3d = obs[0][0] + obs[0][1][j] * 0.5
            else:
                # Single view: place along ray at 0.5m distance from camera center
                pt_3d = obs[0][0] + obs[0][1][j] * 0.5
            corners_3d.append(pt_3d)

        corners_arr = np.stack(corners_3d, axis=0)
        center = np.mean(corners_arr, axis=0)
        v1 = corners_arr[1] - corners_arr[0]
        v2 = corners_arr[3] - corners_arr[0]
        normal_raw = np.cross(v1, v2)
        norm_val = np.linalg.norm(normal_raw)
        normal = (normal_raw / norm_val) if norm_val > 1e-6 else np.array([0.0, 0.0, 1.0])

        results.append({
            "id": int(marker_id),
            "marker_id": int(marker_id),
            "corners": [[float(coord) for coord in pt] for pt in corners_arr],
            "center": [float(coord) for coord in center],
            "normal": [float(coord) for coord in normal],
            "observation_count": len(obs),
        })

    return results


