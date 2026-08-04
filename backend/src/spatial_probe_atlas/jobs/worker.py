from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.compute import CPU_MAPPING_PROFILE, CUDA_MAPPING_PROFILE, REPLAY_MAPPING_PROFILE
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.pipelines.mapping import build_cpu_point_cloud
from spatial_probe_atlas.pipelines.mapping.sfm import build_sift_point_cloud

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
        "code": "MAPPING_WORKER_FAILED",
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
        if specification["type"] != "mapping":
            raise AppError("WORKER_TYPE_UNSUPPORTED", "The worker specification type is not supported.", status_code=422)
        frames = specification["frames"]
        effective = specification.get("effective_compute_profile")
        if effective == REPLAY_MAPPING_PROFILE:
            if not frames or not all(frame.get("source") == "replay" for frame in frames):
                raise AppError("MAPPING_PROFILE_INPUT_MISMATCH", "The replay mapping profile accepts replay frames only.", status_code=422)
            builder = build_cpu_point_cloud
        elif effective == CPU_MAPPING_PROFILE:
            builder = build_sift_point_cloud
        elif effective == CUDA_MAPPING_PROFILE:
            from spatial_probe_atlas.pipelines.mapping.cuda import build_cuda_point_cloud

            builder = build_cuda_point_cloud
        else:
            raise AppError(
                "MAPPING_PROFILE_INVALID",
                "The frozen effective mapping profile is missing or unknown.",
                status_code=422,
                details={"effective_compute_profile": effective},
            )
        result = builder(
            ArtifactStore(Path(specification["data_root"])),
            project_id=specification["project_id"],
            map_id=specification["map_id"],
            job_id=specification["job_id"],
            frames=frames,
            progress=lambda stage, index, count, value, message: atomic_json(
                progress_path,
                {"stage": stage, "stage_index": index, "stage_count": count, "progress": value, "message": message},
            ),
            cancelled=cancel_path.exists,
        )
        result["requested_compute_profile"] = specification["requested_compute_profile"]
        result["effective_compute_profile"] = effective
        atomic_json(result_path, {"result": result})
        return 0
    except Exception as exc:
        atomic_json(result_path, {"error": _error_document(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
