from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from spatial_probe_atlas.compute.cuda import probe_cuda
from spatial_probe_atlas.pipelines.mapping.cuda import _extract_features, _load_verified_models, _match_pair


pytestmark = pytest.mark.hardware


def test_cuda_aliked_lightglue_inference_smoke() -> None:
    if os.environ.get("SPA_RUN_CUDA_TESTS") != "1":
        pytest.skip("set SPA_RUN_CUDA_TESTS=1 for the opt-in CUDA hardware smoke test")
    root = Path(os.environ.get("SPA_DATA_ROOT", Path.home() / "AppData/Local/SpatialProbeAtlas"))
    capability = probe_cuda(root / "models")
    if not capability.available:
        pytest.skip(f"CUDA profile is not ready: {capability.reason_code}")

    import torch

    device = torch.device(f"cuda:{capability.device_index or 0}")
    extractor, matcher, _ = _load_verified_models(root / "models", device)
    rng = np.random.default_rng(417)
    image0 = rng.integers(0, 256, (256, 256, 3), dtype=np.uint8)
    image1 = np.roll(image0, shift=5, axis=1)
    features0 = _extract_features(torch, extractor, image0, device)
    features1 = _extract_features(torch, extractor, image1, device)
    matches = _match_pair(torch, matcher, features0, features1)
    assert len(features0["keypoints"]) <= 4096
    assert len(features1["keypoints"]) <= 4096
    assert len(matches) >= 15
    assert torch.cuda.memory_allocated(device) > 0
