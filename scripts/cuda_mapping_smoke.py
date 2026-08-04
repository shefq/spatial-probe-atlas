from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np

from spatial_probe_atlas.compute.cuda import probe_cuda
from spatial_probe_atlas.pipelines.mapping.cuda import _extract_features, _load_verified_models, _match_pair


def main() -> int:
    parser = argparse.ArgumentParser(description="Verified ALIKED-n16/LightGlue CUDA smoke test")
    parser.add_argument("--require-ready", action="store_true", help="fail instead of skip when the CUDA profile is not ready")
    arguments = parser.parse_args()
    default_root = Path(os.environ.get("LOCALAPPDATA", Path.cwd())) / "SpatialProbeAtlas"
    data_root = Path(os.environ.get("SPA_DATA_ROOT", default_root))
    capability = probe_cuda(data_root / "models")
    if not capability.available:
        print(json.dumps({"status": "FAIL" if arguments.require_ready else "SKIP", "capability": capability.as_dict()}, sort_keys=True))
        return 1 if arguments.require_ready else 0

    try:
        import torch

        device = torch.device(f"cuda:{capability.device_index or 0}")
        extractor, matcher, checksums = _load_verified_models(data_root / "models", device)
        rng = np.random.default_rng(417)
        image0 = rng.integers(0, 256, (256, 256, 3), dtype=np.uint8)
        image1 = np.roll(image0, shift=5, axis=1)
        features0 = _extract_features(torch, extractor, image0, device)
        features1 = _extract_features(torch, extractor, image1, device)
        matches = _match_pair(torch, matcher, features0, features1)
        torch.cuda.synchronize(device)
        if len(matches) < 15:
            raise RuntimeError(f"only {len(matches)} learned matches passed")
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "device": capability.device_name,
                    "compute_capability": capability.compute_capability,
                    "features": [int(len(features0["keypoints"])), int(len(features1["keypoints"]))],
                    "matches": int(len(matches)),
                    "model_checksums": checksums,
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": type(exc).__name__, "message": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
