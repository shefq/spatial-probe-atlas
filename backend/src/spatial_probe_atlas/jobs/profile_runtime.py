from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from spatial_probe_atlas.domain.errors import AppError

from .safe_runtime import RuntimeJobCoordinator as SafeRuntimeJobCoordinator


class RuntimeJobCoordinator(SafeRuntimeJobCoordinator):
    """Safe coordinator that freezes requested/effective compute profiles in worker IPC."""

    def _run_mapping(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        spec = self._job_spec(job_id)
        frames = [self.catalog.get_resource(job["project_id"], "capture_frame", frame_id) for frame_id in spec["frame_ids"]]
        worker_dir = self.artifacts.staging / job_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        specification = worker_dir / "worker-spec.json"
        progress_file = worker_dir / "worker-progress.json"
        result_file = worker_dir / "worker-result.json"
        cancel_file = worker_dir / "cancel.requested"
        cancel_file.unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)
        self.artifacts.atomic_write_json(
            specification,
            {
                "schema_version": 1,
                "type": "mapping",
                "data_root": str(self.artifacts.root),
                "project_id": job["project_id"],
                "map_id": job["owner_id"],
                "job_id": job_id,
                "frames": frames,
                "requested_compute_profile": spec["requested_compute_profile"],
                "effective_compute_profile": spec["effective_compute_profile"],
                "progress_file": str(progress_file),
                "result_file": str(result_file),
                "cancel_file": str(cancel_file),
            },
        )
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[3])
        environment["PYTHONPATH"] = os.pathsep.join([source_root, *[value for value in sys.path if value]])
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen([sys.executable, "-m", "spatial_probe_atlas.jobs.worker", str(specification)], env=environment, creationflags=flags)
        self._register_process(job_id, process)
        last_stage: tuple[Any, ...] | None = None
        try:
            while process.poll() is None:
                if self._shutting_down.is_set():
                    cancel_file.touch(exist_ok=True)
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=2.0)
                    raise InterruptedError("mapping interrupted by application shutdown")
                if self._cancelled(job_id):
                    cancel_file.touch(exist_ok=True)
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=2.0)
                    raise InterruptedError("mapping cancelled")
                if progress_file.is_file():
                    try:
                        value = json.loads(progress_file.read_text(encoding="utf-8"))
                        signature = (value["stage"], value["stage_index"], value["progress"])
                        if signature != last_stage:
                            self._progress(job_id, value["stage"], value["stage_index"], value["stage_count"], value["progress"], value["message"])
                            last_stage = signature
                    except (OSError, ValueError, KeyError):
                        pass
                time.sleep(0.05)
            process.wait()
            if self._shutting_down.is_set():
                raise InterruptedError("mapping interrupted by application shutdown")
            document = json.loads(result_file.read_text(encoding="utf-8")) if result_file.is_file() else None
            if isinstance(document, dict) and isinstance(document.get("error"), dict):
                error = document["error"]
                raise AppError(
                    str(error.get("code") or "MAPPING_WORKER_FAILED"),
                    str(error.get("message") or "The isolated mapping worker failed."),
                    status_code=int(error.get("status_code") or 500),
                    details=error.get("details") or {},
                    retryable=bool(error.get("retryable", True)),
                    suggested_action=error.get("suggested_action"),
                )
            if process.returncode != 0 or not isinstance(document, dict) or "result" not in document:
                raise AppError("MAPPING_WORKER_FAILED", "The isolated mapping worker exited without a valid result.", status_code=500, details={"returncode": process.returncode}, retryable=True)
            return document["result"]
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
            self._unregister_process(job_id, process)

    def _run_mesh(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        worker_dir = self.artifacts.staging / job_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        specification = worker_dir / "worker-spec.json"
        progress_file = worker_dir / "worker-progress.json"
        result_file = worker_dir / "worker-result.json"
        cancel_file = worker_dir / "cancel.requested"
        cancel_file.unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)
        self.artifacts.atomic_write_json(
            specification,
            {
                "schema_version": 1,
                "type": "mesh",
                "data_root": str(self.artifacts.root),
                "project_id": job["project_id"],
                "map_id": job["owner_id"],
                "job_id": job_id,
                "openmvs_bin": job.get("spec", {}).get("openmvs_bin"),
                "progress_file": str(progress_file),
                "result_file": str(result_file),
                "cancel_file": str(cancel_file),
            },
        )
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[3])
        environment["PYTHONPATH"] = os.pathsep.join([source_root, *[value for value in sys.path if value]])
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen([sys.executable, "-m", "spatial_probe_atlas.jobs.mesh_worker", str(specification)], env=environment, creationflags=flags)
        self._register_process(job_id, process)
        last_stage: tuple[Any, ...] | None = None
        try:
            while process.poll() is None:
                if self._shutting_down.is_set():
                    cancel_file.touch(exist_ok=True)
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=2.0)
                    raise InterruptedError("mesh generation interrupted by application shutdown")
                if self._cancelled(job_id):
                    cancel_file.touch(exist_ok=True)
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=2.0)
                    raise InterruptedError("mesh generation cancelled")
                if progress_file.is_file():
                    try:
                        value = json.loads(progress_file.read_text(encoding="utf-8"))
                        signature = (value["stage"], value["stage_index"], value["progress"])
                        if signature != last_stage:
                            self._progress(job_id, value["stage"], value["stage_index"], value["stage_count"], value["progress"], value["message"])
                            last_stage = signature
                    except (OSError, ValueError, KeyError):
                        pass
                time.sleep(0.05)
            process.wait()
            if self._shutting_down.is_set():
                raise InterruptedError("mesh generation interrupted by application shutdown")
            document = json.loads(result_file.read_text(encoding="utf-8")) if result_file.is_file() else None
            if isinstance(document, dict) and isinstance(document.get("error"), dict):
                error = document["error"]
                raise AppError(
                    str(error.get("code") or "MESH_WORKER_FAILED"),
                    str(error.get("message") or "The isolated mesh worker failed."),
                    status_code=int(error.get("status_code") or 500),
                    details=error.get("details") or {},
                    retryable=bool(error.get("retryable", True)),
                    suggested_action=error.get("suggested_action"),
                )
            if process.returncode != 0 or not isinstance(document, dict) or "result" not in document:
                raise AppError("MESH_WORKER_FAILED", "The isolated mesh worker exited without a valid result.", status_code=500, details={"returncode": process.returncode}, retryable=True)
            return document["result"]
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
            self._unregister_process(job_id, process)
