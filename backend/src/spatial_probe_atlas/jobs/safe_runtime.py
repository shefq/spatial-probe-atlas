from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from spatial_probe_atlas.adapters.persistence.database import JobRecord, ResourceRecord, utcnow
from spatial_probe_atlas.domain.errors import AppError

from .runtime import RuntimeJobCoordinator as BaseRuntimeJobCoordinator


class RuntimeJobCoordinator(BaseRuntimeJobCoordinator):
    """Runtime coordinator with owned worker processes and durable interruption state."""

    def __init__(self, database: Any, catalog: Any, artifacts: Any) -> None:
        super().__init__(database, catalog, artifacts)
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._process_lock = threading.Lock()
        self._shutting_down = threading.Event()

    async def start(self) -> None:
        self._recover_live_sessions("application_started_after_an_unclean_live_session")
        await super().start()

    async def shutdown(self) -> None:
        self._shutting_down.set()
        self._recover_live_sessions("application_shutdown_interrupted_live_session")
        with self.database.session() as db:
            active_job_ids = [
                row.id
                for row in db.scalars(
                    select(JobRecord).where(JobRecord.state.in_(["queued", "admitted", "processing", "cancelling"]))
                )
            ]
        await asyncio.to_thread(self._stop_all_processes)
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        # A shutdown is an interruption, not a user cancellation.  Reclassify every job
        # that was active when shutdown began after its worker has been reaped.
        with self.database.session() as db:
            for job_id in active_job_ids:
                row = db.get(JobRecord, job_id)
                if row is None or row.state == "completed":
                    continue
                row.state = "recoverable" if row.checkpoint else "interrupted"
                row.stage = "interrupted"
                row.message = "Application shutdown interrupted this job after its worker was stopped"
                row.error = {"code": "JOB_INTERRUPTED_BY_SHUTDOWN", "retryable": True}
                row.cancel_requested = False
                row.updated_at = utcnow()
        self._tasks.clear()

    def _recover_live_sessions(self, reason: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        with self.database.session() as db:
            sessions = list(
                db.scalars(
                    select(ResourceRecord).where(
                        ResourceRecord.kind == "session",
                        ResourceRecord.state.in_(["running", "paused", "degraded", "stopping"]),
                    )
                )
            )
            for row in sessions:
                payload = dict(row.payload or {})
                payload.update(
                    {
                        "recovery_reason": reason,
                        "interrupted_at": timestamp,
                        "last_active_state": row.state,
                        "active_path": None,
                    }
                )
                row.payload = payload
                row.state = "recoverable"
                row.revision += 1
                row.updated_at = utcnow()

    def _register_process(self, job_id: str, process: subprocess.Popen[Any]) -> None:
        with self._process_lock:
            self._processes[job_id] = process

    def _unregister_process(self, job_id: str, process: subprocess.Popen[Any]) -> None:
        with self._process_lock:
            if self._processes.get(job_id) is process:
                self._processes.pop(job_id, None)

    def _stop_all_processes(self) -> None:
        with self._process_lock:
            processes = list(self._processes.items())
        for job_id, process in processes:
            (self.artifacts.staging / job_id / "cancel.requested").touch(exist_ok=True)
        cooperative_deadline = time.monotonic() + 2.0
        for _, process in processes:
            timeout = max(0.0, cooperative_deadline - time.monotonic())
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        for _, process in processes:
            if process.poll() is not None:
                process.wait()
                continue
            try:
                if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
                process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2.0)

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
                "progress_file": str(progress_file),
                "result_file": str(result_file),
                "cancel_file": str(cancel_file),
            },
        )
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[3])
        environment["PYTHONPATH"] = os.pathsep.join([source_root, *[value for value in sys.path if value]])
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(
            [sys.executable, "-m", "spatial_probe_atlas.jobs.worker", str(specification)],
            env=environment,
            creationflags=flags,
        )
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
            if process.returncode != 0 or not result_file.is_file():
                details = json.loads(result_file.read_text(encoding="utf-8")) if result_file.is_file() else {"error": f"worker exited {process.returncode}"}
                raise AppError("MAPPING_WORKER_FAILED", "The isolated mapping worker failed.", status_code=500, details=details, retryable=True)
            result = json.loads(result_file.read_text(encoding="utf-8"))
            if "error" in result:
                raise AppError("MAPPING_WORKER_FAILED", result["error"], status_code=500, details=result, retryable=True)
            return result["result"]
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
            self._unregister_process(job_id, process)
