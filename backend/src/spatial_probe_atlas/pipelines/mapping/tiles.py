"""Deterministic SPATILE v1 octree construction and publication validation.

The authoritative PLY keeps every source point.  Browser tiles form a proper
hierarchical cut: internal nodes contain a bounded, spatially distributed sample and
leaf nodes contain the complete points for their subtree.  Consequently a viewer can
replace one parent with all of its children without holes or double-counting.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import struct
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from spatial_probe_atlas.domain.errors import AppError


TILE_MAGIC = b"SPATILE1"
TILE_VERSION = 1
TILE_FLAGS_RGB = 1
TILE_HEADER = struct.Struct("<8sHHI6f")
TILE_RECORD_BYTES = 9
DEFAULT_MAX_LEAF_POINTS = 50_000
DEFAULT_MAX_INTERNAL_POINTS = 8_192
DEFAULT_MAX_DEPTH = 10
_TILE_ID = re.compile(r"r[0-7]*\Z")


@dataclass(frozen=True)
class _Node:
    tile_id: str
    indices: np.ndarray
    depth: int
    bounds_min: np.ndarray
    bounds_max: np.ndarray
    children: tuple[str, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_spatile(
    path: Path,
    points: np.ndarray,
    colours: np.ndarray,
    bounds_min: np.ndarray,
    bounds_max: np.ndarray,
) -> str:
    """Write one immutable little-endian SPATILE v1 and return its SHA-256."""

    points = np.asarray(points, dtype=np.float64)
    colours = np.asarray(colours, dtype=np.uint8)
    bounds_min = np.asarray(bounds_min, dtype=np.float64)
    bounds_max = np.asarray(bounds_max, dtype=np.float64)
    if points.ndim != 2 or points.shape[1:] != (3,) or colours.shape != (len(points), 3):
        raise AppError("POINT_TILE_INPUT_INVALID", "Point-tile XYZ/RGB arrays have incompatible shapes.", status_code=500)
    if not len(points) or not np.isfinite(points).all() or not np.isfinite(bounds_min).all() or not np.isfinite(bounds_max).all():
        raise AppError("POINT_TILE_INPUT_INVALID", "Point-tile coordinates and bounds must be non-empty and finite.", status_code=500)
    tolerance = np.maximum(np.abs(bounds_max - bounds_min), 1.0) * 1e-6
    if np.any(bounds_max < bounds_min) or np.any(points < bounds_min - tolerance) or np.any(points > bounds_max + tolerance):
        raise AppError("POINT_TILE_BOUNDS_INVALID", "Point-tile bounds do not contain every encoded point.", status_code=500)
    path.parent.mkdir(parents=True, exist_ok=True)
    extent = np.maximum(bounds_max - bounds_min, 1e-12)
    quantized = np.clip(np.rint((points - bounds_min) / extent * 65535.0), 0, 65535).astype("<u2")
    interleaved = np.empty(
        len(points),
        dtype=[("x", "<u2"), ("y", "<u2"), ("z", "<u2"), ("r", "u1"), ("g", "u1"), ("b", "u1")],
    )
    interleaved["x"], interleaved["y"], interleaved["z"] = quantized[:, 0], quantized[:, 1], quantized[:, 2]
    interleaved["r"], interleaved["g"], interleaved["b"] = colours[:, 0], colours[:, 1], colours[:, 2]
    with path.open("wb") as handle:
        handle.write(
            TILE_HEADER.pack(
                TILE_MAGIC,
                TILE_VERSION,
                TILE_FLAGS_RGB,
                len(points),
                *bounds_min.astype(float),
                *bounds_max.astype(float),
            )
        )
        handle.write(interleaved.tobytes())
        handle.flush()
        os.fsync(handle.fileno())
    return _sha256(path)


def _part1by2(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.uint64) & np.uint64(0x1FFFFF)
    values = (values | (values << np.uint64(32))) & np.uint64(0x1F00000000FFFF)
    values = (values | (values << np.uint64(16))) & np.uint64(0x1F0000FF0000FF)
    values = (values | (values << np.uint64(8))) & np.uint64(0x100F00F00F00F00F)
    values = (values | (values << np.uint64(4))) & np.uint64(0x10C30C30C30C30C3)
    values = (values | (values << np.uint64(2))) & np.uint64(0x1249249249249249)
    return values


def _spatial_order(points: np.ndarray, indices: np.ndarray, low: np.ndarray, high: np.ndarray) -> np.ndarray:
    local = np.asarray(points[indices], dtype=np.float64)
    extent = np.maximum(high - low, 1e-12)
    grid = np.clip(np.floor((local - low) / extent * ((1 << 21) - 1)), 0, (1 << 21) - 1).astype(np.uint64)
    morton = _part1by2(grid[:, 0]) | (_part1by2(grid[:, 1]) << np.uint64(1)) | (_part1by2(grid[:, 2]) << np.uint64(2))
    return indices[np.argsort(morton, kind="stable")]


def _balanced_groups(
    points: np.ndarray,
    indices: np.ndarray,
    low: np.ndarray,
    high: np.ndarray,
    group_count: int,
) -> list[tuple[int, np.ndarray]]:
    ordered = _spatial_order(points, indices, low, high)
    return [(offset, group) for offset, group in enumerate(np.array_split(ordered, group_count)) if len(group)]


def _partition(
    points: np.ndarray,
    indices: np.ndarray,
    low: np.ndarray,
    high: np.ndarray,
    *,
    child_capacity: int,
) -> list[tuple[int, np.ndarray]]:
    midpoint = (low + high) / 2.0
    local = points[indices]
    octants = ((local >= midpoint).astype(np.uint8) * np.asarray([1, 2, 4], dtype=np.uint8)).sum(axis=1)
    groups = [(octant, indices[octants == octant]) for octant in range(8) if np.any(octants == octant)]
    if len(groups) > 1 and all(len(group) <= child_capacity for _, group in groups):
        return groups
    group_count = max(2, math.ceil(len(indices) / child_capacity))
    if group_count > 8:
        raise AppError("POINT_TILE_CAPACITY_INVALID", "The configured octree depth cannot bound every leaf.", status_code=500)
    return _balanced_groups(points, indices, low, high, group_count)


def _representatives(
    points: np.ndarray,
    indices: np.ndarray,
    low: np.ndarray,
    high: np.ndarray,
    maximum: int,
) -> np.ndarray:
    ordered = _spatial_order(points, indices, low, high)
    if len(ordered) <= maximum:
        return ordered
    positions = np.floor((np.arange(maximum, dtype=np.float64) + 0.5) * len(ordered) / maximum).astype(np.int64)
    return ordered[positions]


def _normalise_input(points: np.ndarray, colours: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    points = np.asarray(points, dtype=np.float64)
    colours = np.asarray(colours)
    if points.ndim != 2 or points.shape[1:] != (3,) or not len(points) or not np.isfinite(points).all():
        raise AppError("POINT_CLOUD_INVALID", "The browser point cloud must contain finite XYZ points.", status_code=422)
    if colours.shape != (len(points), 3) or not np.isfinite(colours).all():
        raise AppError("POINT_CLOUD_COLOUR_INVALID", "The browser point cloud must contain one finite RGB value per point.", status_code=422)
    return np.asarray(points, dtype="<f4"), np.clip(np.rint(colours), 0, 255).astype(np.uint8)


def build_octree_manifest(
    map_directory: Path,
    *,
    project_id: str,
    map_id: str,
    points: np.ndarray,
    colours: np.ndarray,
    coordinate_frame: str,
    units: str,
    max_leaf_points: int = DEFAULT_MAX_LEAF_POINTS,
    max_internal_points: int = DEFAULT_MAX_INTERNAL_POINTS,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict[str, Any]:
    """Build deterministic immutable tiles and return their versioned manifest."""

    points, colours = _normalise_input(points, colours)
    if max_leaf_points < 1 or max_internal_points < 1 or not 1 <= max_depth <= 20:
        raise AppError("POINT_TILE_CONFIGURATION_INVALID", "Point-tile budgets or depth are outside safe bounds.", status_code=500)
    if len(points) > max_leaf_points * (8**max_depth):
        raise AppError("POINT_TILE_CAPACITY_INVALID", "The configured octree cannot bound every leaf.", status_code=500)
    root_low, root_high = points.min(axis=0).astype(np.float64), points.max(axis=0).astype(np.float64)
    pending: deque[tuple[str, np.ndarray, int]] = deque([("r", np.arange(len(points), dtype=np.int64), 0)])
    nodes: dict[str, _Node] = {}
    while pending:
        tile_id, indices, depth = pending.popleft()
        local = points[indices]
        low, high = local.min(axis=0).astype(np.float64), local.max(axis=0).astype(np.float64)
        children: tuple[str, ...] = ()
        if len(indices) > max_leaf_points:
            if depth >= max_depth:
                raise AppError("POINT_TILE_CAPACITY_INVALID", "An octree leaf exceeded its configured point bound.", status_code=500)
            child_capacity = max_leaf_points * (8 ** (max_depth - depth - 1))
            groups = _partition(points, indices, low, high, child_capacity=child_capacity)
            children = tuple(f"{tile_id}{octant}" for octant, _ in groups)
            for (octant, child_indices), child_id in zip(groups, children, strict=True):
                del octant
                pending.append((child_id, child_indices, depth + 1))
        nodes[tile_id] = _Node(tile_id, indices, depth, low, high, children)

    tiles_directory = map_directory / "tiles"
    tiles_directory.mkdir(parents=True, exist_ok=True)
    descriptors: dict[str, Any] = {}
    encoded_point_count = 0
    leaf_count = 0
    for tile_id, node in nodes.items():
        is_leaf = not node.children
        encoded_indices = (
            _spatial_order(points, node.indices, node.bounds_min, node.bounds_max)
            if is_leaf
            else _representatives(points, node.indices, node.bounds_min, node.bounds_max, max_internal_points)
        )
        tile_path = tiles_directory / f"{tile_id}.spatile"
        checksum = write_spatile(tile_path, points[encoded_indices], colours[encoded_indices], node.bounds_min, node.bounds_max)
        diagonal = float(np.linalg.norm(node.bounds_max - node.bounds_min))
        geometric_error = diagonal / 65535.0 if is_leaf else diagonal / 2.0
        descriptor = {
            "tile_id": tile_id,
            "uri": f"projects/{project_id}/maps/{map_id}/tiles/{tile_id}.spatile",
            "bounds": {"min": node.bounds_min.astype(float).tolist(), "max": node.bounds_max.astype(float).tolist()},
            "point_count": int(len(encoded_indices)),
            "subtree_point_count": int(len(node.indices)),
            "children": list(node.children),
            "geometric_error": geometric_error,
            "geometric_error_m": geometric_error,
            "depth": node.depth,
            "sample_type": "leaf" if is_leaf else "representative",
            "sha256": checksum,
            "size_bytes": tile_path.stat().st_size,
        }
        descriptors[tile_id] = descriptor
        encoded_point_count += int(len(encoded_indices))
        leaf_count += int(is_leaf)
    manifest: dict[str, Any] = {
        "format": "spatial-probe-atlas-octree",
        "version": 1,
        "coordinate_frame": coordinate_frame,
        "units": units,
        "point_count": int(len(points)),
        "encoded_point_count": encoded_point_count,
        "tile_count": len(descriptors),
        "leaf_tile_count": leaf_count,
        "bounds": {"min": root_low.astype(float).tolist(), "max": root_high.astype(float).tolist()},
        "position_encoding": "uint16_local_bounds",
        "colour_encoding": "rgb8",
        "root_tiles": ["r"],
        "lod": {
            "strategy": "octree_parent_replacement",
            "max_leaf_points": max_leaf_points,
            "max_internal_points": max_internal_points,
            "max_depth": max_depth,
        },
        "tiles": descriptors,
    }
    validate_octree_manifest(map_directory, manifest, project_id=project_id, map_id=map_id)
    return manifest


def _bounds(value: Any) -> tuple[np.ndarray, np.ndarray]:
    try:
        low = np.asarray(value["min"], dtype=np.float64)
        high = np.asarray(value["max"], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as exc:
        raise AppError("POINT_TILE_MANIFEST_INVALID", "A point-tile descriptor has invalid bounds.", status_code=500) from exc
    if low.shape != (3,) or high.shape != (3,) or not np.isfinite(low).all() or not np.isfinite(high).all() or np.any(high < low):
        raise AppError("POINT_TILE_MANIFEST_INVALID", "A point-tile descriptor has non-finite or reversed bounds.", status_code=500)
    return low, high


def validate_octree_manifest(map_directory: Path, manifest: dict[str, Any], *, project_id: str, map_id: str) -> None:
    """Validate hierarchy, binary headers, sizes and every immutable checksum."""

    if manifest.get("format") != "spatial-probe-atlas-octree" or manifest.get("version") != 1:
        raise AppError("POINT_TILE_MANIFEST_INVALID", "The point-cloud manifest format or version is unsupported.", status_code=500)
    descriptors = manifest.get("tiles")
    roots = manifest.get("root_tiles")
    if not isinstance(descriptors, dict) or not descriptors or roots != ["r"] or "r" not in descriptors:
        raise AppError("POINT_TILE_MANIFEST_INVALID", "The point-cloud manifest must contain one root octree.", status_code=500)
    parents: dict[str, str] = {}
    for tile_id, descriptor in descriptors.items():
        if not isinstance(tile_id, str) or not _TILE_ID.fullmatch(tile_id) or descriptor.get("tile_id") != tile_id:
            raise AppError("POINT_TILE_MANIFEST_INVALID", "A point-tile identifier is invalid.", status_code=500)
        children = descriptor.get("children")
        if not isinstance(children, list) or len(children) > 8 or len(set(children)) != len(children):
            raise AppError("POINT_TILE_MANIFEST_INVALID", "A point-tile has an invalid child list.", status_code=500)
        for child_id in children:
            if child_id not in descriptors or not child_id.startswith(tile_id) or len(child_id) != len(tile_id) + 1:
                raise AppError("POINT_TILE_MANIFEST_INVALID", "A point-tile child reference is invalid.", status_code=500)
            if child_id in parents:
                raise AppError("POINT_TILE_MANIFEST_INVALID", "A point-tile has more than one parent.", status_code=500)
            parents[child_id] = tile_id
    if set(parents) != set(descriptors) - {"r"}:
        raise AppError("POINT_TILE_MANIFEST_INVALID", "The point-tile hierarchy contains unreachable nodes.", status_code=500)

    root_subtree_count = 0
    leaf_point_count = 0
    encoded_point_count = 0
    for tile_id, descriptor in descriptors.items():
        low, high = _bounds(descriptor.get("bounds"))
        point_count = descriptor.get("point_count")
        subtree_count = descriptor.get("subtree_point_count")
        error = descriptor.get("geometric_error")
        if not isinstance(point_count, int) or point_count < 1 or not isinstance(subtree_count, int) or subtree_count < point_count:
            raise AppError("POINT_TILE_MANIFEST_INVALID", "A point-tile count is invalid.", status_code=500)
        if not isinstance(error, (int, float)) or not math.isfinite(float(error)) or float(error) < 0:
            raise AppError("POINT_TILE_MANIFEST_INVALID", "A point-tile geometric error is invalid.", status_code=500)
        expected_uri = f"projects/{project_id}/maps/{map_id}/tiles/{tile_id}.spatile"
        if descriptor.get("uri") != expected_uri:
            raise AppError("POINT_TILE_MANIFEST_INVALID", "A point-tile URI is not canonical and relative.", status_code=500)
        tile_path = map_directory / "tiles" / f"{tile_id}.spatile"
        try:
            size = tile_path.stat().st_size
            with tile_path.open("rb") as handle:
                header = handle.read(TILE_HEADER.size)
            magic, version, flags, encoded_count, *header_bounds = TILE_HEADER.unpack(header)
        except (OSError, struct.error) as exc:
            raise AppError("POINT_TILE_MISSING", "A referenced point tile is missing or truncated.", status_code=500, details={"tile_id": tile_id}) from exc
        if magic != TILE_MAGIC or version != TILE_VERSION or flags != TILE_FLAGS_RGB or encoded_count != point_count:
            raise AppError("POINT_TILE_BINARY_INVALID", "A point-tile binary header does not match its descriptor.", status_code=500, details={"tile_id": tile_id})
        if size != TILE_HEADER.size + point_count * TILE_RECORD_BYTES or descriptor.get("size_bytes") != size:
            raise AppError("POINT_TILE_BINARY_INVALID", "A point-tile binary length does not match its descriptor.", status_code=500, details={"tile_id": tile_id})
        header_low, header_high = np.asarray(header_bounds[:3]), np.asarray(header_bounds[3:])
        if not np.allclose(header_low, low, rtol=1e-6, atol=1e-7) or not np.allclose(header_high, high, rtol=1e-6, atol=1e-7):
            raise AppError("POINT_TILE_BINARY_INVALID", "A point-tile binary bound does not match its descriptor.", status_code=500, details={"tile_id": tile_id})
        if _sha256(tile_path) != descriptor.get("sha256"):
            raise AppError("POINT_TILE_CHECKSUM_INVALID", "A point-tile failed immutable checksum validation.", status_code=500, details={"tile_id": tile_id})
        parent_id = parents.get(tile_id)
        if parent_id:
            parent_low, parent_high = _bounds(descriptors[parent_id].get("bounds"))
            tolerance = np.maximum(np.abs(parent_high - parent_low), 1.0) * 1e-6
            if np.any(low < parent_low - tolerance) or np.any(high > parent_high + tolerance):
                raise AppError("POINT_TILE_MANIFEST_INVALID", "A child point-tile lies outside its parent bounds.", status_code=500)
        children = descriptor["children"]
        if children:
            if descriptor.get("sample_type") != "representative" or sum(descriptors[child]["subtree_point_count"] for child in children) != subtree_count:
                raise AppError("POINT_TILE_MANIFEST_INVALID", "An internal point-tile subtree count is inconsistent.", status_code=500)
        else:
            if descriptor.get("sample_type") != "leaf" or subtree_count != point_count:
                raise AppError("POINT_TILE_MANIFEST_INVALID", "A leaf point-tile must contain its complete subtree.", status_code=500)
            leaf_point_count += point_count
        if tile_id == "r":
            root_subtree_count = subtree_count
        encoded_point_count += point_count
    if manifest.get("point_count") != root_subtree_count or leaf_point_count != root_subtree_count:
        raise AppError("POINT_TILE_MANIFEST_INVALID", "The octree leaf counts do not preserve the authoritative point count.", status_code=500)
    if manifest.get("encoded_point_count") != encoded_point_count or manifest.get("tile_count") != len(descriptors):
        raise AppError("POINT_TILE_MANIFEST_INVALID", "The octree manifest summary counts are inconsistent.", status_code=500)
