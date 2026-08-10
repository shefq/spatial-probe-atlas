from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.domain.transforms import solve_similarity
from spatial_probe_atlas.pipelines.aruco import detect_aruco

def align_map_to_aruco(
    artifact_root: Path,
    sfm_dir: Path,
    frames_metadata: list[dict[str, Any]],
    marker_ids: list[int],
    board_layout: dict[int, np.ndarray],
    capture_dir: Path | None = None,
) -> dict[str, Any]:
    import cv2 # type: ignore
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

    board_obj_pts = np.vstack([board_layout[m] for m in marker_ids]).astype(np.float32)
    
    map_camera_centers = []
    aruco_camera_centers = []
    
    import re
    for image in model.images.values():
        match = re.search(r'(\d+)', image.name)
        if not match:
            continue
        idx = int(match.group(1))
        
        frame_meta = None
        if 0 <= idx < len(frames_metadata):
            frame_meta = frames_metadata[idx]
        else:
            frame_meta = next((f for f in frames_metadata if f.get("sequence", -1) == idx), None)
            
        if not frame_meta:
            continue
            
        rgb_artifact = frame_meta.get("rgb_artifact", {})
        if not rgb_artifact:
            continue
            
        rgb_path = artifact_root / rgb_artifact["relative_uri"]
        if not rgb_path.exists():
            continue
            
        width, height = int(frame_meta["width"]), int(frame_meta["height"])
        if rgb_path.suffix == ".rgb8":
            rgb = np.fromfile(rgb_path, dtype=np.uint8).reshape(height, width, 3)
        else:
            rgb = cv2.cvtColor(cv2.imread(str(rgb_path)), cv2.COLOR_BGR2RGB)
            
        camera = model.cameras[image.camera_id]
        k_matrix = camera.calibration_matrix()
        
        # Detect ArUco
        aruco_detections, _ = detect_aruco(rgb, width, height, "DICT_4X4_50", marker_ids)
        if len(aruco_detections) < len(marker_ids):
            continue
            
        img_pts = []
        for m in marker_ids:
            if m not in aruco_detections:
                break
            img_pts.append(aruco_detections[m])
        if len(img_pts) != len(marker_ids):
            continue
            
        img_pts_np = np.vstack(img_pts).astype(np.float32)
        success_aruco, rvec_aruco, tvec_aruco = cv2.solvePnP(board_obj_pts, img_pts_np, k_matrix, None, flags=cv2.SOLVEPNP_ITERATIVE)
        if not success_aruco:
            continue
            
        rot_aruco, _ = cv2.Rodrigues(rvec_aruco)
        t_c_w = np.eye(4); t_c_w[:3, :3] = rot_aruco; t_c_w[:3, 3] = tvec_aruco[:, 0]
        # Camera center in ArUco World Frame
        c_w = (np.linalg.inv(t_c_w))[:3, 3]
        
        # Map center in M0 frame
        c_m0 = image.projection_center() if callable(image.projection_center) else image.projection_center
        
        map_camera_centers.append(c_m0)
        aruco_camera_centers.append(c_w)

    if len(map_camera_centers) < 3:
        raise AppError("ALIGNMENT_VIEWS_INSUFFICIENT", f"At least 3 valid views required for alignment, but only got {len(map_camera_centers)}.", status_code=422)

    # Solve similarity transform from M0 (Map) to W (ArUco World)
    return solve_similarity(map_camera_centers, aruco_camera_centers)
