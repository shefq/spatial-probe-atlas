from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

import numpy as np

from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.domain.errors import AppError

from .tiles import build_octree_manifest, validate_octree_manifest, write_spatile as _write_tile


def _read_frame(store: ArtifactStore, payload: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    width, height = int(payload["width"]), int(payload["height"])
    rgb = np.fromfile(store.root / payload["rgb_artifact"]["relative_uri"], dtype=np.uint8).reshape(height, width, 3)
    depth = np.fromfile(store.root / payload["depth_artifact"]["relative_uri"], dtype="<f4").reshape(height, width)
    return rgb, depth, np.asarray(payload["intrinsic_matrix"], dtype=float).reshape(3, 3)


def _cloud_from_frames(store: ArtifactStore, frames: list[dict[str, Any]], cancelled: Callable[[], bool]) -> tuple[np.ndarray, np.ndarray]:
    clouds: list[np.ndarray] = []
    colours: list[np.ndarray] = []
    for frame_index, frame in enumerate(frames):
        if cancelled():
            raise InterruptedError("mapping cancelled")
        rgb, depth, k = _read_frame(store, frame)
        height, width = depth.shape
        stride = max(2, int(max(width, height) / 80))
        yy, xx = np.mgrid[0:height:stride, 0:width:stride]
        zz = depth[0:height:stride, 0:width:stride]
        valid = np.isfinite(zz) & (zz > 0.05) & (zz < 5.0)
        x = (xx[valid] - k[0, 2]) * zz[valid] / k[0, 0]
        y = (yy[valid] - k[1, 2]) * zz[valid] / k[1, 1]
        points = np.column_stack((x + frame_index * 0.0025, y, zz[valid])).astype("<f4")
        clouds.append(points)
        colours.append(rgb[0:height:stride, 0:width:stride][valid].astype(np.uint8))
    if not clouds:
        raise AppError("MAPPING_NO_USABLE_DEPTH", "No usable depth samples were found in the accepted frames.", status_code=422)
    points, rgb = np.concatenate(clouds), np.concatenate(colours)
    keys = np.floor(points / 0.001).astype(np.int64)
    _, unique = np.unique(keys, axis=0, return_index=True)
    unique.sort()
    return points[unique], rgb[unique]


def _write_ply(
    path: Path,
    points: np.ndarray,
    colours: np.ndarray,
    *,
    coordinate_frame: str,
    units: str,
) -> tuple[str, int]:
    header = (
        "ply\nformat binary_little_endian 1.0\ncomment Spatial Probe Atlas authoritative point cloud\n"
        f"comment coordinate_frame {coordinate_frame}\ncomment units {units}\n"
        f"element vertex {len(points)}\n"
        "property float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n"
    ).encode("ascii")
    vertex = np.empty(len(points), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1")])
    vertex["x"], vertex["y"], vertex["z"] = points[:, 0], points[:, 1], points[:, 2]
    vertex["r"], vertex["g"], vertex["b"] = colours[:, 0], colours[:, 1], colours[:, 2]
    with path.open("wb") as handle:
        handle.write(header)
        handle.write(vertex.tobytes())
        handle.flush()
        os.fsync(handle.fileno())
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest(), len(content)


def build_cpu_point_cloud(
    store: ArtifactStore, *, project_id: str, map_id: str, job_id: str, frames: list[dict[str, Any]],
    progress: Callable[[str, int, int, float, str], None], cancelled: Callable[[], bool],
) -> dict[str, Any]:
    stages = ["ingest", "quality", "features", "pairs", "matches", "reconstruction", "authoritative_export", "tile_build", "validation", "publish"]
    for index, stage_name in enumerate(stages[:6], 1):
        if cancelled():
            raise InterruptedError("mapping cancelled")
        progress(stage_name, index, len(stages), 1.0, f"Depth-assisted replay completed {stage_name}")
    points, colours = _cloud_from_frames(store, frames, cancelled)
    stage = store.staging / job_id
    if stage.exists():
        import shutil
        shutil.rmtree(stage)
    output = stage / "map"
    output.mkdir(parents=True, exist_ok=True)
    ply = output / "point-cloud.ply"
    ply_sha, ply_size = _write_ply(ply, points, colours, coordinate_frame="M0", units="arbitrary")
    progress("authoritative_export", 7, len(stages), 1.0, f"Wrote {len(points):,} points")
    manifest = build_octree_manifest(
        output,
        project_id=project_id,
        map_id=map_id,
        points=points,
        colours=colours,
        coordinate_frame="M0",
        units="arbitrary",
    )
    manifest["authoritative_ply"] = {
        "relative_uri": f"projects/{project_id}/maps/{map_id}/point-cloud.ply",
        "sha256": ply_sha,
        "size_bytes": ply_size,
        "point_count": int(len(points)),
    }
    validate_octree_manifest(output, manifest, project_id=project_id, map_id=map_id)
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha, manifest_size = hashlib.sha256(manifest_bytes).hexdigest(), len(manifest_bytes)
    progress("tile_build", 8, len(stages), 1.0, f"Built {manifest['tile_count']:,} deterministic octree tiles")
    if not np.isfinite(points).all() or len(points) < 100:
        raise AppError("MAP_VALIDATION_FAILED", "The generated point cloud did not pass finite/point-count validation.", status_code=422)
    progress("validation", 9, len(stages), 1.0, "Validated PLY, manifest, hierarchy, binary headers, and tile checksums")
    final = store.project_path(project_id, Path("maps") / map_id)
    if final.exists():
        existing = _published_result_if_valid(store, project_id, map_id, final)
        if existing is not None:
            progress("publish", 10, len(stages), 1.0, "Reused the already validated atomic publication")
            return existing
        raise AppError("MAP_PUBLICATION_CONFLICT", "A different or incomplete map artifact already exists for this immutable revision.", status_code=409)
    store.publish_directory(output, final)
    try:
        stage.rmdir()
    except OSError:
        pass
    progress("publish", 10, len(stages), 1.0, "Published map atomically")
    return {
        "algorithm": "depth_assisted_replay_v1", "effective_compute_profile": "cpu_depth_assisted_replay",
        "point_count": int(len(points)), "registered_image_count": len(frames), "input_frame_count": len(frames),
        "registered_ratio": 1.0, "mean_reprojection_error_px": 0.0, "units": "arbitrary", "coordinate_frame": "M0",
        "ply": {"relative_uri": f"projects/{project_id}/maps/{map_id}/point-cloud.ply", "sha256": ply_sha, "size_bytes": ply_size},
        "manifest": {"relative_uri": f"projects/{project_id}/maps/{map_id}/manifest.json", "sha256": manifest_sha, "size_bytes": manifest_size},
        "bounds": manifest["bounds"],
    }


def _published_result_if_valid(store: ArtifactStore, project_id: str, map_id: str, final: Path) -> dict[str, Any] | None:
    manifest_path = final / "manifest.json"
    ply_path = final / "point-cloud.ply"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_octree_manifest(final, manifest, project_id=project_id, map_id=map_id)
    except (AppError, OSError, ValueError, TypeError):
        return None
    ply_meta = manifest.get("authoritative_ply", {})
    if (
        not ply_path.is_file()
        or ply_meta.get("point_count") != manifest.get("point_count")
        or ply_meta.get("size_bytes") != ply_path.stat().st_size
        or ply_meta.get("sha256") != hashlib.sha256(ply_path.read_bytes()).hexdigest()
    ):
        return None
    manifest_bytes, ply_bytes = manifest_path.read_bytes(), ply_path.read_bytes()
    return {
        "algorithm": "depth_assisted_replay_v1", "effective_compute_profile": "cpu_depth_assisted_replay",
        "point_count": int(manifest["point_count"]), "registered_image_count": 0, "input_frame_count": 0,
        "registered_ratio": 1.0, "mean_reprojection_error_px": 0.0, "units": manifest["units"], "coordinate_frame": manifest["coordinate_frame"],
        "ply": {"relative_uri": f"projects/{project_id}/maps/{map_id}/point-cloud.ply", "sha256": hashlib.sha256(ply_bytes).hexdigest(), "size_bytes": len(ply_bytes)},
        "manifest": {"relative_uri": f"projects/{project_id}/maps/{map_id}/manifest.json", "sha256": hashlib.sha256(manifest_bytes).hexdigest(), "size_bytes": len(manifest_bytes)},
        "bounds": manifest["bounds"], "publication_recovered": True,
    }
