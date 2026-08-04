"""Spawned worker entry point for one immutable legacy-import specification."""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.services.legacy_import import build_legacy_import

from .worker_ipc import atomic_json


def _error_document(error: Exception) -> dict[str, Any]:
    if isinstance(error, AppError):
        return {
            "code": error.code,
            "message": error.message,
            "status_code": error.status_code,
            "details": error.details,
            "retryable": error.retryable,
            "suggested_action": error.suggested_action,
            "traceback": traceback.format_exc(),
        }
    return {
        "code": "LEGACY_IMPORT_WORKER_FAILED",
        "message": str(error),
        "status_code": 500,
        "details": {},
        "retryable": True,
        "suggested_action": "Inspect the durable job and migration staging report, then retry.",
        "traceback": traceback.format_exc(),
    }


def main(specification_path: str) -> int:
    specification = json.loads(Path(specification_path).read_text(encoding="utf-8"))
    result_path = Path(specification["result_file"])
    progress_path = Path(specification["progress_file"])
    cancel_path = Path(specification["cancel_file"])
    try:
        if specification.get("type") != "legacy_import":
            raise AppError("WORKER_TYPE_UNSUPPORTED", "The legacy worker requires a legacy_import specification.", status_code=422)
        result = build_legacy_import(
            ArtifactStore(Path(specification["data_root"])),
            job_id=specification["job_id"],
            project_id=specification["project_id"],
            source_directory=specification["source_directory"],
            requested_project_name=specification.get("requested_project_name"),
            confirm_defaulted_probe_settings=bool(specification.get("confirm_defaulted_probe_settings")),
            progress=lambda stage, index, count, value, message: atomic_json(
                progress_path,
                {"stage": stage, "stage_index": index, "stage_count": count, "progress": value, "message": message},
            ),
            cancelled=cancel_path.exists,
        )
        atomic_json(result_path, {"result": result})
        return 0
    except Exception as error:
        atomic_json(result_path, {"error": _error_document(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
