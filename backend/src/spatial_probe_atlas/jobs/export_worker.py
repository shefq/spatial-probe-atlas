"""Spawned worker entry point for one immutable session-export specification."""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.adapters.persistence.database import Database
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.services import Catalog
from spatial_probe_atlas.services.review_export import run_session_export

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
        "code": "EXPORT_WORKER_FAILED",
        "message": str(error),
        "status_code": 500,
        "details": {},
        "retryable": True,
        "suggested_action": "Inspect the durable export job and retry.",
        "traceback": traceback.format_exc(),
    }


def main(specification_path: str) -> int:
    specification = json.loads(Path(specification_path).read_text(encoding="utf-8"))
    result_path = Path(specification["result_file"])
    progress_path = Path(specification["progress_file"])
    cancel_path = Path(specification["cancel_file"])
    database = Database(specification["database_url"])
    try:
        if specification.get("type") != "session_export":
            raise AppError("WORKER_TYPE_UNSUPPORTED", "The export worker requires a session_export specification.", status_code=422)

        def progress(stage: str, index: int, count: int, value: float, message: str) -> None:
            if cancel_path.exists():
                raise InterruptedError("export cancelled")
            atomic_json(
                progress_path,
                {"stage": stage, "stage_index": index, "stage_count": count, "progress": value, "message": message},
            )

        artifacts = ArtifactStore(Path(specification["data_root"]))
        result = run_session_export(
            Catalog(database, artifacts),
            database,
            artifacts,
            project_id=specification["project_id"],
            session_id=specification["session_id"],
            export_id=specification["export_id"],
            spec=specification["export_spec"],
            progress=progress,
        )
        if cancel_path.exists():
            raise InterruptedError("export cancelled")
        atomic_json(result_path, {"result": result})
        return 0
    except Exception as error:
        atomic_json(result_path, {"error": _error_document(error)})
        return 1
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
