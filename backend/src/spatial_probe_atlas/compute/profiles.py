from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from spatial_probe_atlas.domain.errors import AppError

if TYPE_CHECKING:
    from .cuda import CudaCapability


CPU_MAPPING_PROFILE = "cpu_sift_v1"
CUDA_MAPPING_PROFILE = "cuda_aliked_lightglue_v1"
REPLAY_MAPPING_PROFILE = "depth_assisted_replay_v1"

CUDA_PROFILE_PARAMETERS: dict[str, Any] = {
    "feature_extractor": "ALIKED",
    "aliked_model": "aliked-n16",
    "max_keypoints": 4096,
    "detection_threshold": 0.2,
    "nms_radius": 2,
    "resize_long_edge_px": 1024,
    "matcher": "LightGlue",
    "lightglue_features": "aliked",
    "depth_confidence": 0.95,
    "width_confidence": 0.99,
    "max_attention_layers": 9,
    "filter_threshold": 0.1,
    "sequential_window": 8,
    "retrieval_top_k": 20,
    "bounded_exhaustive_max_frames": 40,
    "minimum_verified_inliers": 15,
    "reconstruction": "pycolmap_incremental",
}

CUDA_REQUIRED_DISTRIBUTIONS: dict[str, str] = {
    "torch": "2.11.0+cu128",
    "kornia": "0.8.3",
    "pycolmap": "4.1.1",
}


@dataclass(frozen=True, slots=True)
class ModelAsset:
    asset_id: str
    filename: str
    sha256: str
    size_bytes: int
    license: str
    url: str


CUDA_MODEL_ASSETS: tuple[ModelAsset, ...] = (
    ModelAsset(
        asset_id="aliked-n16",
        filename="aliked-n16.pth",
        sha256="5be8704840ed662d9d8c561bf7279c222092674e7eb05fd0feab94899e9d82f2",
        size_bytes=2_738_091,
        license="BSD-3-Clause",
        url=(
            "https://raw.githubusercontent.com/Shiaoming/ALIKED/"
            "683d7c65197395c0b3f01ebe76e1084a27e73a65/models/aliked-n16.pth"
        ),
    ),
    ModelAsset(
        asset_id="aliked-lightglue-v0.1-arxiv",
        filename="aliked_lightglue.pth",
        sha256="d975e965b105311a6143194852297dff4f02aea5cc2e10cecfed966ca0e22503",
        size_bytes=47_632_827,
        license="Apache-2.0",
        url="https://github.com/cvg/LightGlue/releases/download/v0.1_arxiv/aliked_lightglue.pth",
    ),
)


@dataclass(frozen=True, slots=True)
class ModelVerification:
    ready: bool
    assets: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "assets": list(self.assets), "errors": list(self.errors)}


def repository_model_manifest() -> Path:
    return Path(__file__).resolve().parents[4] / "models" / "manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model_assets(
    model_root: Path,
    *,
    manifest_path: Path | None = None,
    expected: Iterable[ModelAsset] = CUDA_MODEL_ASSETS,
) -> ModelVerification:
    """Verify immutable metadata and local bytes before a model is deserialized."""

    expected_assets = tuple(expected)
    manifest_path = manifest_path or repository_model_manifest()
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        declared = {str(item.get("id")): item for item in document.get("assets", [])}
    except (OSError, ValueError, TypeError) as exc:
        return ModelVerification(False, (), (f"manifest_invalid:{type(exc).__name__}",))

    for asset in expected_assets:
        declaration = declared.get(asset.asset_id)
        metadata_ok = bool(
            declaration
            and declaration.get("filename") == asset.filename
            and str(declaration.get("sha256", "")).lower() == asset.sha256
            and int(declaration.get("size_bytes", -1)) == asset.size_bytes
            and declaration.get("license") == asset.license
            and declaration.get("url") == asset.url
            and "cuda" in declaration.get("profiles", [])
        )
        path = model_root / asset.filename
        file_ok = False
        observed_hash: str | None = None
        if path.is_file() and path.stat().st_size == asset.size_bytes:
            observed_hash = _sha256(path)
            file_ok = observed_hash == asset.sha256
        if not metadata_ok:
            errors.append(f"{asset.asset_id}:manifest_metadata")
        if not file_ok:
            errors.append(f"{asset.asset_id}:missing_or_checksum")
        rows.append(
            {
                **asdict(asset),
                "path": str(path),
                "metadata_verified": metadata_ok,
                "file_verified": file_ok,
                "observed_sha256": observed_hash,
            }
        )
    return ModelVerification(not errors, tuple(rows), tuple(errors))


def resolve_mapping_profile(
    requested: str,
    capability: CudaCapability,
    *,
    replay_only: bool = False,
) -> str:
    """Resolve once per attempt; never substitute algorithms mid-job."""

    if replay_only:
        return REPLAY_MAPPING_PROFILE
    normalized = requested.strip().lower()
    if normalized in {"cpu", "cpu_sift", CPU_MAPPING_PROFILE}:
        return CPU_MAPPING_PROFILE
    if normalized == "auto":
        return CUDA_MAPPING_PROFILE if capability.available else CPU_MAPPING_PROFILE
    if normalized in {"cuda", "cuda_aliked_lightglue", CUDA_MAPPING_PROFILE}:
        if not capability.available:
            raise AppError(
                "CUDA_PROFILE_NOT_READY",
                "The CUDA mapping profile did not pass runtime, dependency, kernel, and model verification.",
                status_code=503,
                retryable=True,
                details={
                    "requested_profile": CUDA_MAPPING_PROFILE,
                    "retry_profile": CPU_MAPPING_PROFILE,
                    "capability": capability.as_dict(),
                },
                suggested_action="Retry as cpu_sift_v1, or use Diagnostics to repair the CUDA dependency/model checks.",
            )
        return CUDA_MAPPING_PROFILE
    raise AppError(
        "COMPUTE_PROFILE_INVALID",
        "Compute profile must be auto, cpu_sift_v1, or cuda_aliked_lightglue_v1.",
        status_code=422,
        details={"requested_profile": requested},
    )
