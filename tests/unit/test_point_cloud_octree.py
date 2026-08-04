from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import pytest

from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.pipelines.mapping.tiles import build_octree_manifest, validate_octree_manifest


PROJECT_ID = "11111111-1111-4111-8111-111111111111"
MAP_ID = "22222222-2222-4222-8222-222222222222"


def _cloud(count: int = 2_048) -> tuple[np.ndarray, np.ndarray]:
    index = np.arange(count, dtype=np.int64)
    points = np.column_stack(
        (
            ((index * 37) % 101) / 100.0,
            ((index * 53) % 103) / 80.0 - 0.4,
            ((index * 79) % 107) / 50.0 + 0.2,
        )
    ).astype("<f4")
    colours = np.column_stack(((index * 11) % 256, (index * 17) % 256, (index * 23) % 256)).astype(np.uint8)
    return points, colours


def _build(directory: Path, points: np.ndarray, colours: np.ndarray) -> dict:
    return build_octree_manifest(
        directory,
        project_id=PROJECT_ID,
        map_id=MAP_ID,
        points=points,
        colours=colours,
        coordinate_frame="M0",
        units="arbitrary",
        max_leaf_points=64,
        max_internal_points=17,
        max_depth=6,
    )


def test_octree_is_deterministic_hierarchical_and_bounded(tmp_path: Path) -> None:
    points, colours = _cloud()
    first, second = tmp_path / "first", tmp_path / "second"
    manifest = _build(first, points, colours)
    repeated = _build(second, points, colours)

    assert manifest == repeated
    assert manifest["root_tiles"] == ["r"]
    assert manifest["point_count"] == len(points)
    assert manifest["tile_count"] > 1
    assert manifest["leaf_tile_count"] > 1
    assert manifest["tiles"]["r"]["sample_type"] == "representative"
    assert manifest["tiles"]["r"]["point_count"] == 17
    assert manifest["tiles"]["r"]["subtree_point_count"] == len(points)

    leaves = [tile for tile in manifest["tiles"].values() if not tile["children"]]
    internals = [tile for tile in manifest["tiles"].values() if tile["children"]]
    assert sum(tile["point_count"] for tile in leaves) == len(points)
    assert all(tile["point_count"] <= 64 and tile["point_count"] == tile["subtree_point_count"] for tile in leaves)
    assert all(tile["point_count"] <= 17 and tile["sample_type"] == "representative" for tile in internals)
    assert all(tile["geometric_error"] >= 0 and tile["sha256"] for tile in manifest["tiles"].values())

    for tile_id in manifest["tiles"]:
        assert (first / "tiles" / f"{tile_id}.spatile").read_bytes() == (second / "tiles" / f"{tile_id}.spatile").read_bytes()
    validate_octree_manifest(first, manifest, project_id=PROJECT_ID, map_id=MAP_ID)


def test_degenerate_cloud_still_has_bounded_complete_leaves(tmp_path: Path) -> None:
    points = np.repeat(np.asarray([[0.25, -0.5, 1.0]], dtype="<f4"), 513, axis=0)
    colours = np.repeat(np.asarray([[12, 34, 56]], dtype=np.uint8), len(points), axis=0)
    manifest = build_octree_manifest(
        tmp_path,
        project_id=PROJECT_ID,
        map_id=MAP_ID,
        points=points,
        colours=colours,
        coordinate_frame="M0",
        units="arbitrary",
        max_leaf_points=16,
        max_internal_points=7,
        max_depth=3,
    )
    leaves = [tile for tile in manifest["tiles"].values() if not tile["children"]]
    assert leaves
    assert max(tile["point_count"] for tile in leaves) <= 16
    assert sum(tile["point_count"] for tile in leaves) == len(points)
    validate_octree_manifest(tmp_path, manifest, project_id=PROJECT_ID, map_id=MAP_ID)


def test_validation_rejects_checksum_and_hierarchy_tampering(tmp_path: Path) -> None:
    points, colours = _cloud(300)
    manifest = _build(tmp_path, points, colours)
    tile_path = tmp_path / "tiles" / "r.spatile"
    content = bytearray(tile_path.read_bytes())
    content[-1] ^= 0xFF
    tile_path.write_bytes(content)
    with pytest.raises(AppError) as checksum_error:
        validate_octree_manifest(tmp_path, manifest, project_id=PROJECT_ID, map_id=MAP_ID)
    assert checksum_error.value.code == "POINT_TILE_CHECKSUM_INVALID"

    tile_path.write_bytes(bytes(content[:-1]) + bytes([content[-1] ^ 0xFF]))
    invalid = copy.deepcopy(manifest)
    invalid["tiles"]["r"]["children"] = ["r7"]
    with pytest.raises(AppError) as hierarchy_error:
        validate_octree_manifest(tmp_path, invalid, project_id=PROJECT_ID, map_id=MAP_ID)
    assert hierarchy_error.value.code == "POINT_TILE_MANIFEST_INVALID"
