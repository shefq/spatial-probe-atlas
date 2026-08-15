from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from spatial_probe_atlas import __version__
from spatial_probe_atlas.adapters.persistence.database import utcnow
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.observability import log_event
from spatial_probe_atlas.services.legacy_import import publish_legacy_import

from .profile_runtime import RuntimeJobCoordinator as ProfileRuntimeJobCoordinator


logger = logging.getLogger("spatial_probe_atlas.jobs")


class RuntimeJobCoordinator(ProfileRuntimeJobCoordinator):
    """Final v1 coordinator for every isolated heavy/operational worker."""

    async def _run(self, job_id: str) -> None:
        async with self._heavy_lock:
            try:
                self._set(job_id, state="admitted", stage="admission", message="Resource admission passed", progress=0.0)
                self._set(job_id, state="processing", stage="starting", message="Worker started", started_at=utcnow())
                job = self.catalog.get_job(job_id)
                if job["type"] == "mapping":
                    result = await asyncio.to_thread(self._run_mapping, job_id, job)
                    self.catalog.update_resource(job["project_id"], "scene_map", job["owner_id"], payload_patch=result, state="ready_unscaled")
                elif job["type"] == "mesh":
                    result = await asyncio.to_thread(self._run_mesh, job_id, job)
                    # Don't overwrite the state or payload of the map, just update job
                    self.catalog.update_resource(job["project_id"], "scene_map", job["owner_id"], payload_patch={"mesh_available": True})
                elif job["type"] == "session_export":
                    result = await asyncio.to_thread(self._run_export, job_id, job)
                    self.catalog.update_resource(job["project_id"], "export", job["owner_id"], payload_patch=result, state="completed")
                elif job["type"] == "legacy_import":
                    result = await asyncio.to_thread(self._run_legacy_import, job_id, job)
                elif job["type"] in {"support_bundle", "repair_reindex", "data_root_migration"}:
                    result = await asyncio.to_thread(self._run_operation, job_id, job)
                else:
                    raise AppError("JOB_TYPE_UNSUPPORTED", f"Unsupported job type: {job['type']}", status_code=422)
                if self._cancelled(job_id):
                    self._set(job_id, state="cancelled", stage="cancelled", message="Job cancelled", finished_at=utcnow())
                else:
                    self._set(job_id, state="completed", stage="completed", message="Job completed", progress=1.0, result=result, finished_at=utcnow())
            except (InterruptedError, asyncio.CancelledError):
                self._set(job_id, state="cancelled", stage="cancelled", message="Job cancelled", finished_at=utcnow())
            except Exception as exc:
                if isinstance(exc, AppError):
                    error = {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                        "retryable": exc.retryable,
                        "suggested_action": exc.suggested_action,
                    }
                    message = exc.message
                else:
                    error = {
                        "code": "JOB_FAILED",
                        "message": str(exc),
                        "details": {},
                        "retryable": True,
                        "suggested_action": "Inspect the job details and application log, then retry.",
                    }
                    message = str(exc)
                self._set(job_id, state="failed", stage="failed", message=message, error=error, finished_at=utcnow())
            finally:
                self._tasks.pop(job_id, None)

    def _worker_paths(self, job_id: str) -> tuple[Path, Path, Path, Path]:
        worker_dir = self.artifacts.staging / job_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        specification = worker_dir / "worker-spec.json"
        progress_file = worker_dir / "worker-progress.json"
        result_file = worker_dir / "worker-result.json"
        cancel_file = worker_dir / "cancel.requested"
        progress_file.unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)
        cancel_file.unlink(missing_ok=True)
        return specification, progress_file, result_file, cancel_file

    def _run_owned_worker(
        self,
        job_id: str,
        *,
        module: str,
        specification: dict[str, Any],
        failure_code: str,
        failure_message: str,
    ) -> dict[str, Any]:
        specification_path = Path(specification["specification_file"])
        progress_file = Path(specification["progress_file"])
        result_file = Path(specification["result_file"])
        cancel_file = Path(specification["cancel_file"])
        payload = {key: value for key, value in specification.items() if key != "specification_file"}
        self.artifacts.atomic_write_json(specification_path, payload)

        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[3])
        environment["PYTHONPATH"] = os.pathsep.join([source_root, *[value for value in sys.path if value]])
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen([sys.executable, "-m", module, str(specification_path)], env=environment, creationflags=flags)
        self._register_process(job_id, process)
        log_event(
            logger,
            "worker.started",
            f"Started isolated {payload.get('type')} worker",
            job_id=job_id,
            correlation_id=job_id,
            project_id=payload.get("project_id"),
            session_id=payload.get("session_id"),
            compute_mode=payload.get("effective_compute_profile"),
            worker_pid=process.pid,
        )
        last_stage: tuple[Any, ...] | None = None
        last_heartbeat: str | None = None
        try:
            while process.poll() is None:
                if self._shutting_down.is_set() or self._cancelled(job_id):
                    cancel_file.touch(exist_ok=True)
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=2.0)
                    reason = "application shutdown" if self._shutting_down.is_set() else "cancellation"
                    raise InterruptedError(f"worker interrupted by {reason}")
                if progress_file.is_file():
                    try:
                        value = json.loads(progress_file.read_text(encoding="utf-8"))
                        signature = (value["stage"], value["stage_index"], value["progress"])
                        if signature != last_stage:
                            self._progress(
                                job_id,
                                value["stage"],
                                value["stage_index"],
                                value["stage_count"],
                                value["progress"],
                                value["message"],
                            )
                            last_stage = signature
                        elif value.get("heartbeat_at") and value.get("heartbeat_at") != last_heartbeat:
                            self._set(job_id, heartbeat_at=utcnow())
                        if isinstance(value.get("warnings"), list) and value["warnings"]:
                            self._set(job_id, warnings=value["warnings"][:100])
                        if value.get("heartbeat_at"):
                            last_heartbeat = str(value["heartbeat_at"])
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                time.sleep(0.05)
            process.wait()
            if self._shutting_down.is_set() or self._cancelled(job_id):
                raise InterruptedError("worker result was not published after interruption")
            document = json.loads(result_file.read_text(encoding="utf-8")) if result_file.is_file() else None
            if isinstance(document, dict) and isinstance(document.get("error"), dict):
                error = document["error"]
                raise AppError(
                    str(error.get("code") or failure_code),
                    str(error.get("message") or failure_message),
                    status_code=int(error.get("status_code") or 500),
                    details=error.get("details") or {},
                    retryable=bool(error.get("retryable", True)),
                    suggested_action=error.get("suggested_action"),
                )
            if process.returncode != 0 or not isinstance(document, dict) or not isinstance(document.get("result"), dict):
                raise AppError(
                    failure_code,
                    failure_message,
                    status_code=500,
                    details={"returncode": process.returncode},
                    retryable=True,
                )
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
            log_event(
                logger,
                "worker.exited",
                f"Isolated worker exited with code {process.returncode}",
                level=logging.INFO if process.returncode == 0 else logging.WARNING,
                job_id=job_id,
                correlation_id=job_id,
                worker_pid=process.pid,
                returncode=process.returncode,
            )

    def _run_export(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        spec = self._job_spec(job_id)
        specification_file, progress_file, result_file, cancel_file = self._worker_paths(job_id)
        database_url = self.database.engine.url.render_as_string(hide_password=False)
        return self._run_owned_worker(
            job_id,
            module="spatial_probe_atlas.jobs.export_worker",
            specification={
                "specification_file": str(specification_file),
                "schema_version": 1,
                "type": "session_export",
                "database_url": database_url,
                "data_root": str(self.artifacts.root),
                "project_id": job["project_id"],
                "session_id": spec["session_id"],
                "export_id": job["owner_id"],
                "job_id": job_id,
                "export_spec": spec,
                "progress_file": str(progress_file),
                "result_file": str(result_file),
                "cancel_file": str(cancel_file),
            },
            failure_code="EXPORT_WORKER_FAILED",
            failure_message="The isolated export worker failed.",
        )

    def _run_legacy_import(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        spec = self._job_spec(job_id)
        specification_file, progress_file, result_file, cancel_file = self._worker_paths(job_id)
        result = self._run_owned_worker(
            job_id,
            module="spatial_probe_atlas.jobs.legacy_worker",
            specification={
                "specification_file": str(specification_file),
                "schema_version": 1,
                "type": "legacy_import",
                "data_root": str(self.artifacts.root),
                "project_id": spec["project_id"],
                "source_directory": spec["source_directory"],
                "requested_project_name": spec.get("requested_project_name"),
                "confirm_defaulted_probe_settings": bool(spec.get("confirm_defaulted_probe_settings")),
                "job_id": job_id,
                "progress_file": str(progress_file),
                "result_file": str(result_file),
                "cancel_file": str(cancel_file),
            },
            failure_code="LEGACY_IMPORT_WORKER_FAILED",
            failure_message="The isolated legacy import worker failed.",
        )
        if self._shutting_down.is_set() or self._cancelled(job_id):
            raise InterruptedError("legacy import publication interrupted")
        return publish_legacy_import(self.database, self.artifacts, job_id, result)

    def _run_operation(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        spec = self._job_spec(job_id)
        specification_file, progress_file, result_file, cancel_file = self._worker_paths(job_id)
        worker_spec: dict[str, Any] = {
            "specification_file": str(specification_file),
            "schema_version": 1,
            "type": job["type"],
            "application_version": __version__,
            "data_root": str(self.artifacts.root),
            "job_id": job_id,
            "stage_count": 4,
            "progress_file": str(progress_file),
            "result_file": str(result_file),
            "cancel_file": str(cancel_file),
        }
        if job["type"] == "support_bundle":
            worker_spec["include_raw_frames"] = False
        elif job["type"] == "repair_reindex":
            worker_spec["mode"] = "non_destructive_candidate"
        else:
            worker_spec["destination_root"] = spec["destination_root"]
            worker_spec["disk_reserve_bytes"] = max(0, int(spec.get("disk_reserve_bytes", 0)))
        return self._run_owned_worker(
            job_id,
            module="spatial_probe_atlas.jobs.operations_worker",
            specification=worker_spec,
            failure_code=f"{job['type'].upper()}_WORKER_FAILED",
            failure_message=f"The isolated {job['type']} worker failed.",
        )

    def _set(self, job_id: str, **values: Any) -> None:
        super()._set(job_id, **values)
        try:
            snapshot = self.catalog.get_job(job_id)
            spec = self._job_spec(job_id)
        except Exception:
            return
        state = str(snapshot.get("state") or values.get("state") or "updated")
        event = "job.progress" if state == "processing" else f"job.{state}"
        level = logging.ERROR if state == "failed" else logging.WARNING if state in {"cancelled", "interrupted", "recoverable"} else logging.INFO
        duration_ms: float | None = None
        started_at = snapshot.get("started_at")
        if started_at is not None and state in {"completed", "failed", "cancelled", "interrupted", "recoverable"}:
            try:
                duration_ms = max(0.0, (utcnow() - started_at).total_seconds() * 1000.0)
            except (TypeError, ValueError):
                duration_ms = None
        error = snapshot.get("error") if isinstance(snapshot.get("error"), dict) else {}
        log_event(
            logger,
            event,
            str(snapshot.get("message") or state),
            level=level,
            correlation_id=job_id,
            job_id=job_id,
            project_id=snapshot.get("project_id"),
            session_id=spec.get("session_id"),
            duration_ms=duration_ms,
            compute_mode=spec.get("effective_compute_profile") or spec.get("compute_profile"),
            error_code=error.get("code"),
            state=state,
            stage=snapshot.get("stage"),
            stage_index=snapshot.get("stage_index"),
            stage_count=snapshot.get("stage_count"),
            progress=snapshot.get("progress"),
            attempt=snapshot.get("attempt"),
        )

    def cancel(self, job_id: str) -> dict[str, Any]:
        result = super().cancel(job_id)
        log_event(logger, "job.cancel_requested", "Cancellation requested", level=logging.WARNING, correlation_id=job_id, job_id=job_id, project_id=result.get("project_id"))
        return result

    def resume(self, job_id: str) -> dict[str, Any]:
        result = super().resume(job_id)
        log_event(logger, "job.resume_requested", "Job queued for another attempt", correlation_id=job_id, job_id=job_id, project_id=result.get("project_id"), attempt=result.get("attempt"))
        return result


__all__ = ["RuntimeJobCoordinator"]
