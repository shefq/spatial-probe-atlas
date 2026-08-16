from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

from spatial_probe_atlas.pipelines.mapping.align import AlignmentView, refine_similarity_from_views


def _look_at(camera_center_w: np.ndarray, target_w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return the OpenCV-style world-to-camera pose for a camera looking at target."""
    forward = target_w - camera_center_w
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, np.array([0.0, 1.0, 0.0]))
    if np.linalg.norm(right) < 1e-6:
        right = np.cross(forward, np.array([1.0, 0.0, 0.0]))
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    rotation = np.vstack((right, down, forward))
    return rotation, -rotation @ camera_center_w


def test_corner_refinement_uses_partial_marker_views_and_rejects_bad_pnp_view() -> None:
    rng = np.random.default_rng(8)
    k = np.array([[800.0, 0.0, 320.0], [0.0, 805.0, 240.0], [0.0, 0.0, 1.0]])
    # Two markers from a single, rigid virtual board. Views may see only one.
    board = np.array([
        [-0.08, 0.03, 0.0], [-0.02, 0.03, 0.0], [-0.02, -0.03, 0.0], [-0.08, -0.03, 0.0],
        [0.02, 0.03, 0.0], [0.08, 0.03, 0.0], [0.08, -0.03, 0.0], [0.02, -0.03, 0.0],
    ])
    scale = 0.72
    rotation_w_m0 = Rotation.from_euler("xyz", [8.0, -11.0, 17.0], degrees=True).as_matrix()
    translation_w_m0 = np.array([0.18, -0.09, 0.04])
    camera_centres_w = [
        np.array([-0.16, -0.12, 0.43]), np.array([0.16, -0.12, 0.44]),
        np.array([-0.20, 0.11, 0.50]), np.array([0.20, 0.10, 0.49]),
        np.array([0.00, -0.23, 0.57]), np.array([0.03, 0.22, 0.54]),
        np.array([-0.24, 0.01, 0.46]),
    ]
    views: list[AlignmentView] = []
    for index, centre_w in enumerate(camera_centres_w):
        r_c_w, _ = _look_at(centre_w, np.zeros(3))
        centre_m0 = rotation_w_m0.T @ (centre_w - translation_w_m0) / scale
        r_c_m0 = r_c_w @ rotation_w_m0
        t_c_m0 = -r_c_m0 @ centre_m0
        selected = board[:4] if index % 2 == 0 else board[4:]
        points_c = selected @ r_c_w.T - (r_c_w @ centre_w)
        projected = points_c @ k.T
        pixels = projected[:, :2] / projected[:, 2, None]
        pixels += rng.normal(0.0, 0.18, pixels.shape)
        if index == 1:
            pixels[0] += np.array([9.0, -7.0])  # One bad detected corner.
        pnp_centre_w = centre_w.copy()
        if index == len(camera_centres_w) - 1:
            pnp_centre_w += np.array([0.12, -0.07, 0.08])
        views.append(AlignmentView(
            view_id=str(index), camera_rotation=r_c_m0, camera_translation=t_c_m0,
            intrinsics=k, board_points=selected, image_points=pixels,
            map_camera_center=centre_m0, board_camera_center=pnp_centre_w,
            marker_ids=(10 if index % 2 == 0 else 11,),
        ))

    result = refine_similarity_from_views(views)

    assert result["method"] == "robust_corner_reprojection_v1"
    assert result["view_count"] == 7
    assert result["inlier_view_count"] == 6
    assert result["corner_inlier_count"] == 23
    assert result["rms_reprojection_error_px"] < 0.6
    assert abs(result["scale"] - scale) < 0.01
    assert np.allclose(result["translation"], translation_w_m0, atol=0.01)
    assert np.allclose(np.asarray(result["rotation"]).reshape(3, 3), rotation_w_m0, atol=0.015)
