from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.pipelines.mapping.openmvs import build_openmvs_mesh

from .worker_ipc import atomic_json


def _error_document(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, AppError):
        return {
            "code": exc.code,
            "message": exc.message,
            "status_code": exc.status_code,
            "details": exc.details,
            "retryable": exc.retryable,
            "suggested_action": exc.suggested_action,
            "traceback": traceback.format_exc(),
        }
    return {
        "code": "MESH_WORKER_FAILED",
        "message": str(exc),
        "status_code": 500,
        "details": {},
        "retryable": True,
        "suggested_action": None,
        "traceback": traceback.format_exc(),
    }


def main(specification_path: str) -> int:
    specification = json.loads(Path(specification_path).read_text(encoding="utf-8"))
    result_path, progress_path, cancel_path = (
        Path(specification[key]) for key in ("result_file", "progress_file", "cancel_file")
    )
    try:
        if specification["type"] != "mesh":
            raise AppError("WORKER_TYPE_UNSUPPORTED", "The worker specification type is not supported.", status_code=422)
            
        store = ArtifactStore(Path(specification["data_root"]))
        map_dir = store.project_path(specification["project_id"], Path("maps") / specification["map_id"])
        
        if not map_dir.exists():
            raise AppError("MAP_NOT_FOUND", "The target map artifact was not found.", status_code=404)

        openmvs_bin = Path(specification["openmvs_bin"]) if specification.get("openmvs_bin") else None

        result = build_openmvs_mesh(
            map_dir=map_dir,
            progress=lambda stage, index, count, value, message: atomic_json(
                progress_path,
                {"stage": stage, "stage_index": index, "stage_count": count, "progress": value, "message": message},
            ),
            cancelled=cancel_path.exists,
            openmvs_bin=openmvs_bin,
        )
        atomic_json(result_path, {"result": result})
        return 0
    except Exception as exc:
        atomic_json(result_path, {"error": _error_document(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
