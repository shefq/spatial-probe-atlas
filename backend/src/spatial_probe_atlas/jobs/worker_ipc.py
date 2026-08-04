from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def atomic_json(path: Path, value: Any) -> None:
    """Publish worker IPC JSON atomically, tolerating short Windows reader locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(50):
            try:
                os.replace(temporary_path, path)
                return
            except (PermissionError, OSError):
                if attempt == 49:
                    raise
                time.sleep(0.01)
    finally:
        temporary_path.unlink(missing_ok=True)


__all__ = ["atomic_json"]
