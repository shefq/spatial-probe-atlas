from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.ports.camera import NormalizedCameraFrame

from .cpu import CpuTrackingPipeline, TrackingState


class LightGlueTrackingPipeline(CpuTrackingPipeline):
    """PnP localizer backed by Deep Learning ALIKED + LightGlue and a pycolmap SFM model."""

    def __init__(self, sfm_info: dict[str, Any], similarity: dict[str, Any], calibration: dict[str, Any], artifact_root: Path) -> None:
        try:
            import cv2  # type: ignore
            import pycolmap
            import torch
            from spatial_probe_atlas.compute.cuda import probe_cuda
            from spatial_probe_atlas.pipelines.mapping.cuda import _load_verified_models, _prepare_image_tensor
        except Exception as exc:
            raise AppError("LIGHTGLUE_TRACKING_UNAVAILABLE", "Torch, Kornia, and pycolmap are required for LightGlue tracking.", status_code=503) from exc

        uri = sfm_info.get("relative_uri")
        if not isinstance(uri, str):
            raise AppError("SFM_MODEL_UNAVAILABLE", "The active map has no SFM model URI.", status_code=409)

        root = artifact_root.resolve()
        sfm_dir = (root / Path(uri)).resolve()
        if not sfm_dir.is_dir():
            raise AppError("SFM_MODEL_MISSING", "The active map SFM directory is missing.", status_code=409)

        npz_path = sfm_dir / "aliked_features.npz"
        if not npz_path.is_file():
            raise AppError("SFM_FEATURES_MISSING", "The active map is missing the ALIKED features NPZ file.", status_code=409)

        try:
            self.scale = float(similarity["scale"])
            self.rotation = np.asarray(similarity["rotation"], dtype=float).reshape(3, 3)
            self.translation = np.asarray(similarity["translation"], dtype=float).reshape(3)
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError("REGISTRATION_SIMILARITY_INVALID", "Tracking requires the active registration similarity.", status_code=409) from exc

        if not np.isfinite(self.scale) or self.scale <= 0 or not np.isfinite(self.rotation).all() or not np.isfinite(self.translation).all():
            raise AppError("REGISTRATION_SIMILARITY_INVALID", "Tracking requires a finite positive registration similarity.", status_code=409)

        # Initialize base state
        self.cv2 = cv2
        self.pycolmap = pycolmap
        self.torch = torch
        self._prepare_image_tensor = _prepare_image_tensor
        self.calibration = calibration
        self.camera_state = TrackingState()
        self.probe_state = TrackingState()
        
        # Locate pycolmap reconstruction model directory
        rec_dir = sfm_dir
        if not (rec_dir / "cameras.bin").exists() and not (rec_dir / "cameras.txt").exists():
            candidates = list(sfm_dir.glob("**/cameras.bin")) + list(sfm_dir.glob("**/cameras.txt"))
            if candidates:
                rec_dir = candidates[0].parent

        # Load pycolmap model
        try:
            self.model = pycolmap.Reconstruction(str(rec_dir))
        except Exception as exc:
            raise AppError("SFM_MODEL_INVALID", f"Could not load pycolmap Reconstruction from {rec_dir}: {exc}", status_code=500) from exc

        # Initialize CUDA device and models
        capability = probe_cuda(root / "models")
        if not capability.available:
            raise AppError("CUDA_NOT_AVAILABLE", "CUDA is not available for LightGlue tracking.", status_code=503)
            
        self.device = torch.device(f"cuda:{capability.device_index or 0}")
        try:
            self.extractor, self.matcher, _ = _load_verified_models(root / "models", self.device)
        except Exception as exc:
            raise AppError("MODELS_LOAD_FAILED", "Failed to load ALIKED and LightGlue models.", status_code=500) from exc
            
        # Load reference ALIKED features into memory
        self.reference_features = []
        try:
            with np.load(npz_path, allow_pickle=False) as values:
                sorted_images = sorted(
                    self.model.images.values(),
                    key=lambda img: int(img.num_points3D() if callable(getattr(img, "num_points3D", None)) else getattr(img, "num_points3D", 0)),
                    reverse=True,
                )
                
                for img in sorted_images[:15]: 
                    name = img.name
                    if f"{name}_keypoints" not in values:
                        continue
                        
                    kps = values[f"{name}_keypoints"]
                    desc = values[f"{name}_descriptors"]
                    img_size = values[f"{name}_image_size"]
                    
                    self.reference_features.append({
                        "name": name,
                        "image_id": img.image_id,
                        "keypoints_np": kps,
                        "keypoints": torch.from_numpy(kps).unsqueeze(0).to(self.device),
                        "descriptors": torch.from_numpy(desc).unsqueeze(0).to(self.device),
                        "image_size": torch.from_numpy(img_size).to(self.device)
                    })
        except Exception as exc:
            raise AppError("SFM_FEATURES_INVALID", "Failed to parse ALIKED features NPZ file.", status_code=500) from exc

        if not self.reference_features:
            raise AppError("SFM_FEATURES_EMPTY", "No reference features could be matched with the SFM model.", status_code=500)


    def _localize(self, frame: NormalizedCameraFrame) -> tuple[np.ndarray | None, int, float, str | None]:
        # Convert RGB buffer to Image Tensor
        rgb = np.frombuffer(frame.rgb, dtype=np.uint8).reshape(frame.height, frame.width, 3)
        
        # We need _prepare_image_tensor from mapping.cuda
        tensor, scale, processed_width, processed_height = self._prepare_image_tensor(self.torch, rgb, self.device)
        
        # Extract features
        with self.torch.inference_mode():
            features = self.extractor(tensor)[0]
            
        q_kps_tensor = features.keypoints
        if scale != 1.0:
            q_kps_tensor = q_kps_tensor * scale
            
        q_kps_np = q_kps_tensor.detach().cpu().numpy()
        q_desc = features.descriptors.unsqueeze(0)
        q_size = self.torch.tensor([[frame.width, frame.height]], dtype=self.torch.float32, device=self.device)
        
        q_kps_unsqueeze = q_kps_tensor.unsqueeze(0)
        
        best: tuple[np.ndarray | None, int, float, str | None] = (None, 0, math.inf, "insufficient_reference_matches")
        k_matrix = np.asarray(frame.intrinsic_matrix, dtype=float).reshape(3, 3)
        
        for ref in self.reference_features:
            payload = {
                "image0": {
                    "keypoints": q_kps_unsqueeze,
                    "descriptors": q_desc,
                    "image_size": q_size,
                },
                "image1": {
                    "keypoints": ref["keypoints"],
                    "descriptors": ref["descriptors"],
                    "image_size": ref["image_size"],
                },
            }
            
            with self.torch.inference_mode():
                result = self.matcher(payload)
                
            matches = result["matches"][0].detach().cpu().numpy().astype(np.int32)
            if matches.ndim != 2 or len(matches) < 6:
                continue
                
            ref_img = self.model.images[ref["image_id"]]
            pts2D = ref_img.points2D
            
            all_pts2d = []
            all_pts3d = []
            
            for q_idx, r_idx in matches:
                if 0 <= r_idx < len(pts2D):
                    p2d = pts2D[r_idx]
                    if p2d.has_point3D() and p2d.point3D_id in self.model.points3D:
                        pt3d_m0 = self.model.points3D[p2d.point3D_id].xyz
                        pt3d_w = self.scale * (self.rotation @ pt3d_m0) + self.translation
                        
                        all_pts2d.append(q_kps_np[q_idx])
                        all_pts3d.append(pt3d_w)
                    
            if len(all_pts2d) < 6:
                continue
                
            object_points = np.asarray(all_pts3d, dtype=np.float32)
            image_points = np.asarray(all_pts2d, dtype=np.float32)
            
            success, rvec, tvec, inliers = self.cv2.solvePnPRansac(
                object_points, image_points, k_matrix, None, 
                iterationsCount=200, reprojectionError=3.0, confidence=0.999, flags=self.cv2.SOLVEPNP_EPNP
            )
            
            inlier_count = 0 if not success or inliers is None else len(inliers)
            if not success or inlier_count < 6:
                continue
                
            projected, _ = self.cv2.projectPoints(object_points[inliers[:, 0]], rvec, tvec, k_matrix, None)
            error = float(np.sqrt(np.mean(np.sum((projected[:, 0] - image_points[inliers[:, 0]]) ** 2, axis=1))))
            
            rotation_c_w, _ = self.cv2.Rodrigues(rvec)
            t_c_w = np.eye(4)
            t_c_w[:3, :3] = rotation_c_w
            t_c_w[:3, 3] = tvec[:, 0]
            t_w_c = np.linalg.inv(t_c_w)
            
            if inlier_count > best[1] or (inlier_count == best[1] and error < best[2]):
                best = (t_w_c, inlier_count, error, None)
                
        return best
