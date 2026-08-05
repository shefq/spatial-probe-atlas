from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _default_data_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "SpatialProbeAtlas"
    return Path(tempfile.gettempdir()) / "SpatialProbeAtlas"


@dataclass(slots=True, frozen=True)
class Settings:
    data_root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "info"
    compute_profile: str = "auto"
    bootstrap_token: str | None = None
    allow_test_host: bool = False
    frontend_dist: Path | None = None
    min_mapping_frames: int = 7
    disk_reserve_bytes: int = 10 * 1024**3
    telemetry_rate_hz: float = 2.0

    @classmethod
    def from_env(cls) -> "Settings":
        repository = Path(__file__).resolve().parents[4]
        frontend = repository / "frontend" / "dist"
        return cls(
            data_root=Path(os.environ.get("SPA_DATA_ROOT", _default_data_root())).expanduser().resolve(),
            host=os.environ.get("SPA_HOST", "127.0.0.1"),
            port=int(os.environ.get("SPA_PORT", "8765")),
            log_level=os.environ.get("SPA_LOG_LEVEL", "info").lower(),
            compute_profile=os.environ.get("SPA_COMPUTE_PROFILE", "auto").lower(),
            bootstrap_token=os.environ.get("SPA_BOOTSTRAP_TOKEN") or None,
            allow_test_host=os.environ.get("SPA_ALLOW_TEST_HOST", "").strip().lower() in {"1", "true", "yes"},
            frontend_dist=frontend,
            min_mapping_frames=max(3, int(os.environ.get("SPA_MIN_MAPPING_FRAMES", "7"))),
        )

    def ensure_directories(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        for name in ("projects", "models", "cache", "logs", "temp", "support", ".staging"):
            (self.data_root / name).mkdir(parents=True, exist_ok=True)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_root.joinpath('app.db').as_posix()}"
