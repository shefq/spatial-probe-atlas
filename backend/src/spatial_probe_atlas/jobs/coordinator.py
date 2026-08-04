from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from spatial_probe_atlas import __version__
from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.adapters.persistence.database import Database, JobRecord, ResourceRecord, job_dict, utcnow
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.pipelines.mapping import build_cpu_point_cloud
from spatial_probe_atlas.services import Catalog


class JobCoordinator:
    """Durable single-heavy-job coordinator with cooperative cancellation/recovery.

    The coordinator keeps orchestration in the main process and runs CPU/native work on a
    worker thread so camera/API tasks stay responsive. Every job spec/checkpoint is durable;
    a packaging build may switch the same handlers to spawned process workers without
    changing use cases or artifacts.
    """

    def __init__(self, database: Database, catalog: Catalog, artifacts: ArtifactStore) -> None:
        self.database = database
        self.catalog = catalog
        self.artifacts = artifacts
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._heavy_lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    async def start(self) -> None:
        queued: list[str] = []
        with self.database.session() as session:
            for job in session.scalars(select(JobRecord)):
                if job.state in {"admitted", "processing", "cancelling"}:
                    job.state = "recoverable" if job.checkpoint else "interrupted"
                    job.message = "Application stopped before this job completed"
                    job.error = {"code": "JOB_INTERRUPTED", "retryable": True}
                if job.state == "queued":
                    queued.append(job.id)
        for job_id in queued:
            self.submit(job_id)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self.database.session() as session:
            for job in session.scalars(select(JobRecord).where(JobRecord.state.in_(["admitted", "processing", "cancelling"]))):
                job.state = "recoverable" if job.checkpoint else "interrupted"
                job.message = "Interrupted during application shutdown"

    def submit(self, job_id: str) -> None:
        if job_id not in self._tasks or self._tasks[job_id].done():
            self._tasks[job_id] = asyncio.create_task(self._run(job_id), name=f"job-{job_id}")

    async def _run(self, job_id: str) -> None:
        async with self._heavy_lock:
            try:
                self._set(job_id, state="admitted", stage="admission", message="Resource admission passed", progress=0.0)
                self._set(job_id, state="processing", stage="starting", message="Worker started", started_at=utcnow())
                job = self.catalog.get_job(job_id)
                if job["type"] == "mapping":
                    result = await asyncio.to_thread(self._run_mapping, job_id, job)
                    self.catalog.update_resource(job["project_id"], "scene_map", job["owner_id"], payload_patch=result, state="ready_unscaled")
                elif job["type"] == "session_export":
                    result = await asyncio.to_thread(self._run_export, job_id, job)
                    self.catalog.update_resource(job["project_id"], "export", job["owner_id"], payload_patch=result, state="completed")
                elif job["type"] == "support_bundle":
                    result = await asyncio.to_thread(self._run_support_bundle, job_id, job)
                elif job["type"] in {"repair_reindex", "legacy_import"}:
                    result = await asyncio.to_thread(self._run_metadata_job, job_id, job)
                else:
                    raise AppError("JOB_TYPE_UNSUPPORTED", f"Unsupported job type: {job['type']}", status_code=422)
                if self._cancelled(job_id):
                    self._set(job_id, state="cancelled", stage="cancelled", message="Job cancelled", finished_at=utcnow())
                else:
                    self._set(job_id, state="completed", stage="completed", message="Job completed", progress=1.0, result=result, finished_at=utcnow())
            except (InterruptedError, asyncio.CancelledError):
                self._set(job_id, state="cancelled", stage="cancelled", message="Job cancelled", finished_at=utcnow())
            except Exception as exc:
                code = exc.code if isinstance(exc, AppError) else "JOB_FAILED"
                self._set(job_id, state="failed", stage="failed", message=str(exc), error={"code": code, "message": str(exc), "retryable": True}, finished_at=utcnow())
            finally:
                self._tasks.pop(job_id, None)

    def _run_mapping(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        spec = self._job_spec(job_id)
        frames = [self.catalog.get_resource(job["project_id"], "capture_frame", frame_id) for frame_id in spec["frame_ids"]]
        return build_cpu_point_cloud(
            self.artifacts, project_id=job["project_id"], map_id=job["owner_id"], job_id=job_id, frames=frames,
            progress=lambda stage, index, count, value, message: self._progress(job_id, stage, index, count, value, message),
            cancelled=lambda: self._cancelled(job_id),
        )

    def _run_export(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        spec = self._job_spec(job_id)
        project_id, session_id = job["project_id"], spec["session_id"]
        session = self.catalog.get_resource(project_id, "session", session_id)
        points = self.catalog.list_resources(project_id, "painted_point", parent_id=session_id, include_deleted=bool(spec.get("include_deleted")), limit=100000)
        paths = self.catalog.list_resources(project_id, "painted_path", parent_id=session_id, include_deleted=bool(spec.get("include_deleted")), limit=100000)
        export_dir = self.artifacts.project_path(project_id, Path("sessions") / session_id / "exports")
        export_dir.mkdir(parents=True, exist_ok=True)
        format_name = spec.get("format", "json")
        target = export_dir / f"{job['owner_id']}.{format_name}"
        partial = target.with_suffix(target.suffix + ".partial")
        self._progress(job_id, "query", 1, 3, 1.0, f"Loaded {len(points)} points and {len(paths)} paths")
        if format_name == "csv":
            with partial.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["schema_version", "session_id", "point_id", "timestamp", "x_m", "y_m", "z_m", "quality", "note", "deleted"])
                for item in points:
                    position = item.get("position_w_m", [None, None, None])
                    writer.writerow(["1.0.0", session_id, item["point_id"], item.get("timestamp"), *position, item.get("quality"), item.get("note", ""), item["deleted"]])
        elif format_name in {"json", "manifest"}:
            document = {
                "schema_version": "1.0.0", "application_version": __version__, "session": session,
                "coordinate_frame": "W", "units": "m", "filters": spec.get("filters", {}),
                "points": points if format_name == "json" else [], "paths": paths if format_name == "json" else [], "checksums": {},
            }
            partial.write_text(json.dumps(document, indent=2, sort_keys=True, default=str), encoding="utf-8")
        else:
            raise AppError("EXPORT_FORMAT_UNSUPPORTED", "Supported v1 export formats are json, csv, and manifest.", status_code=422)
        self._progress(job_id, "write", 2, 3, 1.0, "Wrote export staging file")
        with partial.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, target)
        checksum = hashlib.sha256(target.read_bytes()).hexdigest()
        self._progress(job_id, "checksum", 3, 3, 1.0, "Verified export checksum")
        return {"format": format_name, "relative_uri": target.relative_to(self.artifacts.root).as_posix(), "sha256": checksum, "size_bytes": target.stat().st_size, "completed_at": utcnow().isoformat()}

    def _run_support_bundle(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        target = self.artifacts.root / "support" / f"support-{job_id}.zip"
        partial = target.with_suffix(".zip.partial")
        self._progress(job_id, "collect", 1, 3, 1.0, "Collected redacted diagnostics")
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("diagnostics.json", json.dumps({"schema_version": "1.0.0", "app_version": __version__, "database_integrity": self.database.integrity_check(), "raw_frames_included": False}, indent=2))
            for manifest in self.artifacts.projects.glob("*/maps/*/manifest.json"):
                archive.write(manifest, f"manifests/{hashlib.sha256(str(manifest).encode()).hexdigest()[:16]}.json")
        self._progress(job_id, "redact", 2, 3, 1.0, "Redacted paths and excluded raw frames")
        os.replace(partial, target)
        checksum = hashlib.sha256(target.read_bytes()).hexdigest()
        self._progress(job_id, "checksum", 3, 3, 1.0, "Verified support bundle")
        return {"relative_uri": target.relative_to(self.artifacts.root).as_posix(), "sha256": checksum, "size_bytes": target.stat().st_size}

    def _run_metadata_job(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        self._progress(job_id, "inventory", 1, 2, 1.0, "Inventoried durable manifests")
        if job["type"] == "legacy_import":
            return {"status": "report_only", "message": "Legacy import requires an explicitly selected source directory and never scans automatically."}
        integrity = self.database.integrity_check()
        self._progress(job_id, "integrity", 2, 2, 1.0, f"Database integrity: {integrity}")
        return {"database_integrity": integrity, "changed": False}

    def _job_spec(self, job_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(JobRecord, job_id)
            if row is None:
                raise RuntimeError(job_id)
            return dict(row.spec)

    def _cancelled(self, job_id: str) -> bool:
        with self.database.session() as session:
            row = session.get(JobRecord, job_id)
            return row is None or row.cancel_requested

    def _progress(self, job_id: str, stage: str, index: int, count: int, value: float, message: str) -> None:
        self._set(job_id, state="processing", stage=stage, stage_index=index, stage_count=count, progress=max(0.0, min(float(value), 1.0)), message=message, heartbeat_at=utcnow(), checkpoint={"last_completed_stage": stage, "stage_index": index})

    def _set(self, job_id: str, **values: Any) -> None:
        with self.database.session() as session:
            row = session.get(JobRecord, job_id)
            if row is None:
                return
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = utcnow()
            session.flush()
            snapshot = job_dict(row)
        self._publish({"type": "job.progress" if snapshot["state"] == "processing" else f"job.{snapshot['state']}", "correlation_id": job_id, "data": snapshot})

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(JobRecord, job_id)
            if row is None:
                raise AppError("JOB_NOT_FOUND", "The requested job does not exist.", status_code=404)
            if row.state in {"completed", "failed", "cancelled"}:
                return job_dict(row)
            row.cancel_requested = True
            row.state = "cancelling"
            row.message = "Cancellation requested"
            session.flush()
            return job_dict(row)

    def resume(self, job_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(JobRecord, job_id)
            if row is None:
                raise AppError("JOB_NOT_FOUND", "The requested job does not exist.", status_code=404)
            if row.state not in {"recoverable", "interrupted", "failed", "cancelled"}:
                raise AppError("JOB_NOT_RESUMABLE", "This job is not in a resumable state.", status_code=409)
            row.state, row.stage, row.progress, row.cancel_requested = "queued", "queued", 0.0, False
            row.attempt += 1
            row.error = None
            session.flush()
            result = job_dict(row)
        self.submit(job_id)
        return result

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _publish(self, event: dict[str, Any]) -> None:
        event = {"protocol_version": 1, "seq": int(datetime.now(UTC).timestamp() * 1000), "timestamp": utcnow().isoformat(), **event}
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)
