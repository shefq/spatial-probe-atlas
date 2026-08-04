from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from spatial_probe_atlas.domain.validation import safe_relative_path


class ArtifactStore:
    def __init__(self, data_root: Path) -> None:
        self.root = data_root.resolve()
        self.projects = self.root / "projects"
        self.staging = self.root / ".staging"

    def project_dir(self, project_id: str) -> Path:
        path = safe_relative_path(self.projects, Path(project_id))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def project_path(self, project_id: str, relative: str | Path) -> Path:
        return safe_relative_path(self.project_dir(project_id), Path(relative))

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def atomic_write_bytes(self, path: Path, content: bytes) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        return {"relative_uri": path.relative_to(self.root).as_posix(), "sha256": self.sha256(path), "size_bytes": path.stat().st_size}

    def atomic_write_json(self, path: Path, value: Any) -> dict[str, Any]:
        # UTC datetime values are normalized by the transport/persistence layer and emitted
        # as ISO strings in immutable job specs/manifests.
        return self.atomic_write_bytes(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=lambda item: item.isoformat() if hasattr(item, "isoformat") else str(item)).encode("utf-8"))

    def publish_directory(self, staging: Path, final: Path) -> None:
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            raise FileExistsError(final)
        os.replace(staging, final)

    def clone_project(self, source_id: str, target_id: str) -> None:
        source = self.project_dir(source_id)
        target = self.projects / target_id
        if target.exists():
            raise FileExistsError(target)
        shutil.copytree(source, target)
