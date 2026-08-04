from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import shutil
from pathlib import Path
from typing import Any, Callable

import numpy as np

from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.compute.cuda import is_cuda_out_of_memory, probe_cuda
from spatial_probe_atlas.compute.profiles import (
    CPU_MAPPING_PROFILE,
    CUDA_MAPPING_PROFILE,
    CUDA_MODEL_ASSETS,
    CUDA_PROFILE_PARAMETERS,
    CUDA_REQUIRED_DISTRIBUTIONS,
    verify_model_assets,
)
from spatial_probe_atlas.domain.errors import AppError

from .cpu import _write_ply
from .tiles import build_octree_manifest, validate_octree_manifest


ProgressCallback = Callable[[str, int, int, float, str], None]
CancellationCallback = Callable[[], bool]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raise_if_cancelled(cancelled: CancellationCallback) -> None:
    if cancelled():
        raise InterruptedError("mapping cancelled")


def _load_rgb(store: ArtifactStore, frame: dict[str, Any]) -> np.ndarray:
    width, height = int(frame["width"]), int(frame["height"])
    path = store.root / frame["rgb_artifact"]["relative_uri"]
    expected = width * height * 3
    raw = np.fromfile(path, dtype=np.uint8)
    if raw.size != expected:
        raise AppError(
            "MAPPING_FRAME_CORRUPT",
            "A mapping frame RGB artifact does not match its declared dimensions.",
            status_code=422,
            details={"frame_id": frame.get("id"), "expected_bytes": expected, "actual_bytes": int(raw.size)},
        )
    return raw.reshape(height, width, 3)


def _retrieval_descriptor(image: np.ndarray) -> np.ndarray:
    channels = []
    for channel in range(3):
        histogram, _ = np.histogram(image[..., channel], bins=16, range=(0, 256))
        channels.append(histogram.astype(np.float64))
    descriptor = np.concatenate(channels)
    norm = np.linalg.norm(descriptor)
    return descriptor / norm if norm > 0 else descriptor


def generate_candidate_pairs(images: list[np.ndarray]) -> list[tuple[int, int]]:
    """Apply the recorded bounded-exhaustive/sequence/retrieval policy."""

    count = len(images)
    if count <= int(CUDA_PROFILE_PARAMETERS["bounded_exhaustive_max_frames"]):
        return [(left, right) for left in range(count) for right in range(left + 1, count)]
    pairs: set[tuple[int, int]] = set()
    window = int(CUDA_PROFILE_PARAMETERS["sequential_window"])
    for left in range(count):
        for right in range(left + 1, min(count, left + window + 1)):
            pairs.add((left, right))
    descriptors = np.stack([_retrieval_descriptor(image) for image in images])
    similarity = descriptors @ descriptors.T
    top_k = min(int(CUDA_PROFILE_PARAMETERS["retrieval_top_k"]), count - 1)
    for left in range(count):
        order = np.argsort(-similarity[left])
        for right in order:
            right = int(right)
            if right == left:
                continue
            pairs.add((min(left, right), max(left, right)))
            if sum(left in pair for pair in pairs) >= top_k + window:
                break
    return sorted(pairs)


def _load_verified_models(model_root: Path, device: Any) -> tuple[Any, Any, dict[str, str]]:
    verification = verify_model_assets(model_root)
    if not verification.ready:
        raise AppError(
            "CUDA_MODELS_INVALID",
            "CUDA model files are missing or failed immutable checksum verification.",
            status_code=503,
            retryable=True,
            details={"verification": verification.as_dict(), "retry_profile": CPU_MAPPING_PROFILE},
            suggested_action="Run setup.bat to restore verified assets, or retry this map with cpu_sift_v1.",
        )
    import torch
    from kornia.feature.aliked import ALIKED
    from kornia.feature.lightglue import LightGlue

    paths = {asset.asset_id: model_root / asset.filename for asset in CUDA_MODEL_ASSETS}
    extractor = ALIKED(
        model_name="aliked-n16",
        max_num_keypoints=int(CUDA_PROFILE_PARAMETERS["max_keypoints"]),
        detection_threshold=float(CUDA_PROFILE_PARAMETERS["detection_threshold"]),
        nms_radius=int(CUDA_PROFILE_PARAMETERS["nms_radius"]),
    )
    extractor_state = torch.load(paths["aliked-n16"], map_location="cpu", weights_only=True)
    if isinstance(extractor_state, dict) and "state_dict" in extractor_state:
        extractor_state = extractor_state["state_dict"]
    extractor.load_state_dict(extractor_state, strict=False)
    extractor = extractor.eval().to(device)

    matcher = LightGlue(
        features=None,
        input_dim=128,
        depth_confidence=float(CUDA_PROFILE_PARAMETERS["depth_confidence"]),
        width_confidence=float(CUDA_PROFILE_PARAMETERS["width_confidence"]),
        n_layers=int(CUDA_PROFILE_PARAMETERS["max_attention_layers"]),
        filter_threshold=float(CUDA_PROFILE_PARAMETERS["filter_threshold"]),
    )
    matcher_state = torch.load(
        paths["aliked-lightglue-v0.1-arxiv"], map_location="cpu", weights_only=True
    )
    if isinstance(matcher_state, dict) and "state_dict" in matcher_state:
        matcher_state = matcher_state["state_dict"]
    for index in range(int(CUDA_PROFILE_PARAMETERS["max_attention_layers"])):
        matcher_state = {
            key.replace(f"self_attn.{index}", f"transformers.{index}.self_attn").replace(
                f"cross_attn.{index}", f"transformers.{index}.cross_attn"
            ): value
            for key, value in matcher_state.items()
        }
    matcher.load_state_dict(matcher_state, strict=False)
    matcher = matcher.eval().to(device)
    return extractor, matcher, {asset.asset_id: asset.sha256 for asset in CUDA_MODEL_ASSETS}


def _prepare_image_tensor(torch: Any, image: np.ndarray, device: Any) -> tuple[Any, float, int, int]:
    import torch.nn.functional as functional

    height, width = image.shape[:2]
    tensor = torch.from_numpy(np.ascontiguousarray(image)).to(device=device, dtype=torch.float32)
    tensor = tensor.permute(2, 0, 1).unsqueeze(0) / 255.0
    limit = int(CUDA_PROFILE_PARAMETERS["resize_long_edge_px"])
    scale = max(height, width) / limit if max(height, width) > limit else 1.0
    if scale > 1.0:
        target_height = max(32, int(round(height / scale)))
        target_width = max(32, int(round(width / scale)))
        tensor = functional.interpolate(tensor, size=(target_height, target_width), mode="bilinear", align_corners=False)
    else:
        target_height, target_width = height, width
    return tensor, scale, target_width, target_height


def _extract_features(
    torch: Any,
    extractor: Any,
    image: np.ndarray,
    device: Any,
) -> dict[str, Any]:
    tensor, scale, processed_width, processed_height = _prepare_image_tensor(torch, image, device)
    with torch.inference_mode():
        features = extractor(tensor)[0]
    keypoints = features.keypoints
    if scale != 1.0:
        keypoints = keypoints * scale
    return {
        "keypoints": keypoints,
        "descriptors": features.descriptors,
        "scores": features.keypoint_scores,
        "image_size": torch.tensor([[image.shape[1], image.shape[0]]], dtype=torch.float32, device=device),
        "processed_size": [processed_width, processed_height],
    }


def _match_pair(torch: Any, matcher: Any, left: dict[str, Any], right: dict[str, Any]) -> np.ndarray:
    payload = {
        "image0": {
            "keypoints": left["keypoints"].unsqueeze(0),
            "descriptors": left["descriptors"].unsqueeze(0),
            "image_size": left["image_size"],
        },
        "image1": {
            "keypoints": right["keypoints"].unsqueeze(0),
            "descriptors": right["descriptors"].unsqueeze(0),
            "image_size": right["image_size"],
        },
    }
    with torch.inference_mode():
        result = matcher(payload)
    matches = result["matches"][0].detach().cpu().numpy().astype(np.uint32, copy=False)
    return np.ascontiguousarray(matches)


def _write_colmap_inputs(
    pycolmap: Any,
    database_path: Path,
    image_dir: Path,
    image_names: list[str],
    frames: list[dict[str, Any]],
    features: list[dict[str, Any]],
    matches: dict[tuple[int, int], np.ndarray],
) -> dict[str, int]:
    with pycolmap.Database.open(database_path):
        pass
    pycolmap.import_images(
        database_path,
        image_dir,
        pycolmap.CameraMode.PER_IMAGE,
        image_names=image_names,
        options={"camera_model": "PINHOLE"},
    )
    with pycolmap.Database.open(database_path) as database:
        images = {image.name: image for image in database.read_all_images()}
        if set(images) != set(image_names):
            raise AppError("COLMAP_IMAGE_IMPORT_FAILED", "pycolmap did not import the complete frozen frame revision.", status_code=500)
        image_ids: dict[str, int] = {}
        for index, name in enumerate(image_names):
            image = images[name]
            image_ids[name] = int(image.image_id)
            camera = database.read_camera(image.camera_id)
            matrix = np.asarray(frames[index]["intrinsic_matrix"], dtype=np.float64).reshape(3, 3)
            camera.params = np.asarray([matrix[0, 0], matrix[1, 1], matrix[0, 2], matrix[1, 2]])
            database.update_camera(camera)
            observed = np.asarray(database.read_camera(image.camera_id).params, dtype=np.float64)
            if not np.allclose(observed, camera.params, rtol=0, atol=1e-9):
                raise AppError("COLMAP_INTRINSICS_MISMATCH", "pycolmap did not preserve exact per-frame PINHOLE intrinsics.", status_code=500)
            # hloc/COLMAP use pixel-centre coordinates offset by +0.5.
            keypoints = features[index]["keypoints"].detach().cpu().numpy().astype(np.float32) + 0.5
            database.write_keypoints(image.image_id, np.ascontiguousarray(keypoints))
        for (left, right), values in matches.items():
            database.write_matches(image_ids[image_names[left]], image_ids[image_names[right]], values)
    return image_ids


def _publish_map(
    store: ArtifactStore,
    *,
    project_id: str,
    map_id: str,
    job_id: str,
    output: Path,
    points: np.ndarray,
    colours: np.ndarray,
    registered_images: int,
    input_images: int,
    errors: list[float],
    track_lengths: list[int],
    dependency_versions: dict[str, str],
    model_checksums: dict[str, str],
    progress: ProgressCallback,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    ply_sha, ply_size = _write_ply(output / "point-cloud.ply", points, colours, coordinate_frame="M0", units="arbitrary")
    progress("authoritative_export", 7, 10, 1.0, "Wrote authoritative binary little-endian PLY")
    manifest = build_octree_manifest(
        output,
        project_id=project_id,
        map_id=map_id,
        points=points,
        colours=colours,
        coordinate_frame="M0",
        units="arbitrary",
    )
    manifest["compute_profile"] = CUDA_MAPPING_PROFILE
    manifest["authoritative_ply"] = {
        "relative_uri": f"projects/{project_id}/maps/{map_id}/point-cloud.ply",
        "sha256": ply_sha,
        "size_bytes": ply_size,
        "point_count": int(len(points)),
    }
    validate_octree_manifest(output, manifest, project_id=project_id, map_id=map_id)
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest_sha = _sha256(manifest_path)
    progress("tile_build", 8, 10, 1.0, f"Built {manifest['tile_count']:,} deterministic octree tiles")
    progress("validation", 9, 10, 1.0, "Validated the largest connected COLMAP component, hierarchy, binary headers, and checksums")
    final = store.project_path(project_id, Path("maps") / map_id)
    if final.exists():
        raise AppError("MAP_PUBLICATION_CONFLICT", "A map artifact already exists for this immutable revision.", status_code=409)
    store.publish_directory(output, final)
    progress("publish", 10, 10, 1.0, "Published map atomically")
    mean_error = float(np.mean(errors)) if errors else None
    return {
        "algorithm": "kornia_aliked_n16_lightglue_pycolmap_v1",
        "requested_compute_profile": CUDA_MAPPING_PROFILE,
        "effective_compute_profile": CUDA_MAPPING_PROFILE,
        "retry_profile_on_failure": CPU_MAPPING_PROFILE,
        "profile_parameters": dict(CUDA_PROFILE_PARAMETERS),
        "dependency_versions": dependency_versions,
        "model_checksums": model_checksums,
        "point_count": int(len(points)),
        "registered_image_count": registered_images,
        "input_frame_count": input_images,
        "registered_ratio": registered_images / input_images,
        "mean_reprojection_error_px": mean_error,
        "mean_track_length": float(np.mean(track_lengths)) if track_lengths else None,
        "units": "arbitrary",
        "coordinate_frame": "M0",
        "ply": {
            "relative_uri": f"projects/{project_id}/maps/{map_id}/point-cloud.ply",
            "sha256": ply_sha,
            "size_bytes": ply_size,
        },
        "manifest": {
            "relative_uri": f"projects/{project_id}/maps/{map_id}/manifest.json",
            "sha256": manifest_sha,
            "size_bytes": (final / "manifest.json").stat().st_size,
        },
        "sfm": {
            "relative_uri": f"projects/{project_id}/maps/{map_id}/sfm",
            "database_sha256": _sha256(final / "sfm" / "database.db"),
        },
        "bounds": manifest["bounds"],
    }


def build_cuda_point_cloud(
    store: ArtifactStore,
    *,
    project_id: str,
    map_id: str,
    job_id: str,
    frames: list[dict[str, Any]],
    progress: ProgressCallback,
    cancelled: CancellationCallback,
) -> dict[str, Any]:
    """Build an ALIKED-n16/LightGlue map; never falls through to CPU in-process."""

    capability = probe_cuda(store.root / "models")
    if not capability.available:
        raise AppError(
            "CUDA_PROFILE_NOT_READY",
            "The CUDA mapping attempt did not pass the complete capability gate.",
            status_code=503,
            retryable=True,
            details={"capability": capability.as_dict(), "retry_profile": CPU_MAPPING_PROFILE},
            suggested_action="Create a new retry using cpu_sift_v1, or repair CUDA from Diagnostics.",
        )
    if len(frames) < 2:
        raise AppError("MAPPING_FRAMES_INSUFFICIENT", "CUDA SfM requires at least two accepted frames.", status_code=422)

    try:
        import cv2
        import pycolmap
        import torch

        device = torch.device(f"cuda:{capability.device_index or 0}")
        extractor, matcher, model_checksums = _load_verified_models(store.root / "models", device)
        dependency_versions = {
            name: importlib.metadata.version(name) for name in CUDA_REQUIRED_DISTRIBUTIONS
        }
        work_root = store.staging / job_id / "cuda-work"
        if work_root.exists():
            shutil.rmtree(work_root)
        image_dir, output = work_root / "images", work_root / "map"
        sfm_dir = output / "sfm"
        models_dir = sfm_dir / "models"
        image_dir.mkdir(parents=True, exist_ok=True)
        models_dir.mkdir(parents=True, exist_ok=True)

        images: list[np.ndarray] = []
        image_names: list[str] = []
        for index, frame in enumerate(frames):
            _raise_if_cancelled(cancelled)
            image = _load_rgb(store, frame)
            name = f"frame-{index:06d}.png"
            if not cv2.imwrite(str(image_dir / name), cv2.cvtColor(image, cv2.COLOR_RGB2BGR)):
                raise AppError("MAPPING_IMAGE_WRITE_FAILED", "Could not stage a validated RGB frame for pycolmap.", status_code=500)
            images.append(image)
            image_names.append(name)
        progress("ingest", 1, 10, 1.0, f"Validated {len(frames)} frame artifacts and exact per-frame intrinsics")
        pairs = generate_candidate_pairs(images)
        progress("quality", 2, 10, 1.0, f"Generated {len(pairs)} bounded sequence/retrieval candidate pairs")

        feature_rows: list[dict[str, Any]] = []
        for image in images:
            _raise_if_cancelled(cancelled)
            feature_rows.append(_extract_features(torch, extractor, image, device))
        feature_count = sum(int(row["keypoints"].shape[0]) for row in feature_rows)
        if sum(int(row["keypoints"].shape[0]) >= 50 for row in feature_rows) < 2:
            raise AppError("SFM_FEATURES_INSUFFICIENT", "Fewer than two frames contain enough ALIKED features.", status_code=422)
        progress("features", 3, 10, 1.0, f"Extracted {feature_count:,} ALIKED-n16 features on CUDA")
        progress("pairs", 4, 10, 1.0, f"Selected {len(pairs)} candidate pairs using the recorded profile")

        pair_matches: dict[tuple[int, int], np.ndarray] = {}
        retained = 0
        for pair_index, (left, right) in enumerate(pairs):
            _raise_if_cancelled(cancelled)
            values = _match_pair(torch, matcher, feature_rows[left], feature_rows[right])
            if len(values) >= int(CUDA_PROFILE_PARAMETERS["minimum_verified_inliers"]):
                pair_matches[(left, right)] = values
                retained += len(values)
            if pair_index % 25 == 0:
                torch.cuda.synchronize(device)
        if not pair_matches:
            raise AppError(
                "SFM_MATCHES_INSUFFICIENT",
                "LightGlue did not retain any candidate pair with at least 15 matches.",
                status_code=422,
                suggested_action="Capture more textured overlapping views or retry with the CPU SIFT profile.",
            )
        progress("matches", 5, 10, 1.0, f"Retained {retained:,} LightGlue matches across {len(pair_matches)} pairs")

        database_path = sfm_dir / "database.db"
        _write_colmap_inputs(pycolmap, database_path, image_dir, image_names, frames, feature_rows, pair_matches)
        pairs_path = sfm_dir / "pairs.txt"
        pairs_path.write_text(
            "".join(f"{image_names[left]} {image_names[right]}\n" for left, right in pair_matches),
            encoding="utf-8",
        )
        pycolmap.verify_matches(
            database_path,
            pairs_path,
            options={"ransac": {"max_num_trials": 20_000, "min_inlier_ratio": 0.1}},
        )
        _raise_if_cancelled(cancelled)
        reconstructions = pycolmap.incremental_mapping(
            database_path,
            image_dir,
            models_dir,
            options={
                "min_num_matches": int(CUDA_PROFILE_PARAMETERS["minimum_verified_inliers"]),
                "multiple_models": True,
                "max_num_models": 5,
                "min_model_size": 2,
                "num_threads": min(os.cpu_count() or 1, 16),
            },
        )
        if not reconstructions:
            raise AppError("SFM_RECONSTRUCTION_FAILED", "pycolmap could not reconstruct a connected component.", status_code=422)
        reconstruction = max(reconstructions.values(), key=lambda value: int(value.num_reg_images()))
        registered = int(reconstruction.num_reg_images())
        point_rows = list(reconstruction.points3D.values())
        points = np.asarray([np.asarray(point.xyz, dtype=np.float64) for point in point_rows], dtype="<f4")
        colours = np.asarray([np.asarray(point.color, dtype=np.uint8) for point in point_rows], dtype=np.uint8)
        finite = np.isfinite(points).all(axis=1)
        points, colours = points[finite], colours[finite]
        errors = [float(point.error) for point, valid in zip(point_rows, finite) if valid and math.isfinite(float(point.error))]
        track_lengths = [int(point.track.length()) for point, valid in zip(point_rows, finite) if valid]
        progress("reconstruction", 6, 10, 1.0, f"Reconstructed {registered}/{len(frames)} cameras and {len(points):,} sparse points")
        if len(points) < 100 or registered < max(2, int(len(frames) * 0.3)):
            raise AppError(
                "SFM_VALIDATION_FAILED",
                "CUDA SfM registered too few cameras or finite points.",
                status_code=422,
                details={"registered_images": registered, "point_count": len(points)},
            )
        shutil.rmtree(image_dir, ignore_errors=True)
        result = _publish_map(
            store,
            project_id=project_id,
            map_id=map_id,
            job_id=job_id,
            output=output,
            points=points,
            colours=colours,
            registered_images=registered,
            input_images=len(frames),
            errors=errors,
            track_lengths=track_lengths,
            dependency_versions=dependency_versions,
            model_checksums=model_checksums,
            progress=progress,
        )
        shutil.rmtree(work_root, ignore_errors=True)
        return result
    except AppError:
        raise
    except InterruptedError:
        raise
    except Exception as exc:
        if is_cuda_out_of_memory(exc):
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
            raise AppError(
                "CUDA_MAPPING_OUT_OF_MEMORY",
                "The CUDA mapping attempt exhausted VRAM; no CPU algorithm was substituted in this attempt.",
                status_code=503,
                retryable=True,
                details={"attempt_profile": CUDA_MAPPING_PROFILE, "retry_profile": CPU_MAPPING_PROFILE},
                suggested_action="Create a clean retry with cpu_sift_v1 or reduce the accepted frame set.",
            ) from exc
        raise AppError(
            "CUDA_MAPPING_FAILED",
            "The verified CUDA mapping process failed before publication.",
            status_code=500,
            retryable=True,
            details={"attempt_profile": CUDA_MAPPING_PROFILE, "retry_profile": CPU_MAPPING_PROFILE, "exception": type(exc).__name__},
            suggested_action="Inspect the job diagnostic, then retry cleanly with cpu_sift_v1 if needed.",
        ) from exc
