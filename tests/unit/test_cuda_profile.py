from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from spatial_probe_atlas.compute.cuda import CudaCapability, is_cuda_out_of_memory, probe_cuda
from spatial_probe_atlas.compute.profiles import (
    CPU_MAPPING_PROFILE,
    CUDA_MAPPING_PROFILE,
    CUDA_MODEL_ASSETS,
    REPLAY_MAPPING_PROFILE,
    ModelAsset,
    resolve_mapping_profile,
    verify_model_assets,
)
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.pipelines.mapping.cuda import generate_candidate_pairs


def _capability(available: bool) -> CudaCapability:
    return CudaCapability(state="cuda_ready" if available else "cpu_only", available=available)


def test_profile_resolution_is_attempt_scoped_and_explicit() -> None:
    assert resolve_mapping_profile("auto", _capability(False)) == CPU_MAPPING_PROFILE
    assert resolve_mapping_profile("auto", _capability(True)) == CUDA_MAPPING_PROFILE
    assert resolve_mapping_profile("cuda", _capability(True)) == CUDA_MAPPING_PROFILE
    assert resolve_mapping_profile("cpu", _capability(True)) == CPU_MAPPING_PROFILE
    assert resolve_mapping_profile("cuda", _capability(False), replay_only=True) == REPLAY_MAPPING_PROFILE

    with pytest.raises(AppError) as error:
        resolve_mapping_profile("cuda", _capability(False))
    assert error.value.code == "CUDA_PROFILE_NOT_READY"
    assert error.value.details["retry_profile"] == CPU_MAPPING_PROFILE


def test_model_verification_rejects_missing_and_tampered_bytes(tmp_path: Path) -> None:
    good = b"immutable-model"
    expected = ModelAsset(
        asset_id="fixture",
        filename="fixture.bin",
        sha256=hashlib.sha256(good).hexdigest(),
        size_bytes=len(good),
        license="BSD-3-Clause",
        url="https://example.invalid/immutable/fixture.bin",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "assets": [
                    {
                        "id": expected.asset_id,
                        "filename": expected.filename,
                        "sha256": expected.sha256,
                        "size_bytes": expected.size_bytes,
                        "license": expected.license,
                        "url": expected.url,
                        "profiles": ["cuda"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    model_root = tmp_path / "models"
    model_root.mkdir()
    assert not verify_model_assets(model_root, manifest_path=manifest, expected=[expected]).ready
    (model_root / expected.filename).write_bytes(good)
    assert verify_model_assets(model_root, manifest_path=manifest, expected=[expected]).ready
    (model_root / expected.filename).write_bytes(b"tampered-model")
    assert not verify_model_assets(model_root, manifest_path=manifest, expected=[expected]).ready


def test_release_manifest_matches_code_pinned_assets() -> None:
    manifest_path = Path(__file__).resolve().parents[2] / "models" / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = {item["id"]: item for item in document["assets"]}
    for expected in CUDA_MODEL_ASSETS:
        assert declared[expected.asset_id]["sha256"] == expected.sha256
        assert declared[expected.asset_id]["size_bytes"] == expected.size_bytes
        assert declared[expected.asset_id]["url"] == expected.url
        assert declared[expected.asset_id]["license"] == expected.license
        assert declared[expected.asset_id]["profiles"] == ["cuda"]


def test_driver_without_verified_models_is_not_cuda_ready(tmp_path: Path) -> None:
    capability = probe_cuda(
        tmp_path,
        driver_probe=lambda: ("570.65", "Fixture GPU"),
        installed_versions={"torch": "2.11.0+cu128", "kornia": "0.8.3", "pycolmap": "4.1.1"},
    )
    assert capability.state == "cuda_incompatible"
    assert capability.reason_code == "CUDA_MODELS_INVALID"
    assert not capability.available


def test_cuda_oom_classification_is_typed_without_importing_torch() -> None:
    assert is_cuda_out_of_memory(RuntimeError("CUDA out of memory. Tried to allocate 64 MiB"))
    assert not is_cuda_out_of_memory(MemoryError("host allocation failed"))


def test_candidate_pair_policy_is_bounded_and_deterministic() -> None:
    images = [np.full((8, 8, 3), index, dtype=np.uint8) for index in range(6)]
    assert generate_candidate_pairs(images) == [
        (left, right) for left in range(6) for right in range(left + 1, 6)
    ]
    large = [np.full((4, 4, 3), index % 255, dtype=np.uint8) for index in range(41)]
    first = generate_candidate_pairs(large)
    second = generate_candidate_pairs(large)
    assert first == second
    assert (0, 1) in first and (0, 8) in first
    assert len(first) < 41 * 40 // 2
