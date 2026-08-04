from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path
from typing import Any

import numpy as np

from spatial_probe_atlas import __version__
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.adapters.persistence.database import utcnow

from .coordinator import JobCoordinator as BaseJobCoordinator


class RuntimeJobCoordinator(BaseJobCoordinator):
    """Production coordinator additions: thread-safe submission and spawned map worker."""

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await super().start()

    def submit(self, job_id: str) -> None:
        def schedule() -> None:
            if job_id not in self._tasks or self._tasks[job_id].done():
                self._tasks[job_id] = self._loop.create_task(self._run(job_id), name=f"job-{job_id}")
        try:
            if asyncio.get_running_loop() is self._loop:
                schedule()
            else:
                self._loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            self._loop.call_soon_threadsafe(schedule)

    def _run_mapping(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        spec = self._job_spec(job_id)
        frames = [self.catalog.get_resource(job["project_id"], "capture_frame", frame_id) for frame_id in spec["frame_ids"]]
        worker_dir = self.artifacts.staging / job_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        specification = worker_dir / "worker-spec.json"
        progress_file, result_file, cancel_file = worker_dir / "worker-progress.json", worker_dir / "worker-result.json", worker_dir / "cancel.requested"
        self.artifacts.atomic_write_json(specification, {"schema_version": 1, "type": "mapping", "data_root": str(self.artifacts.root), "project_id": job["project_id"], "map_id": job["owner_id"], "job_id": job_id, "frames": frames, "progress_file": str(progress_file), "result_file": str(result_file), "cancel_file": str(cancel_file)})
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[3])
        environment["PYTHONPATH"] = os.pathsep.join([source_root, *[value for value in sys.path if value]])
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen([sys.executable, "-m", "spatial_probe_atlas.jobs.worker", str(specification)], env=environment, creationflags=flags)
        last_stage: tuple[Any, ...] | None = None
        while process.poll() is None:
            if self._cancelled(job_id):
                cancel_file.touch()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
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
        if process.returncode != 0 or not result_file.is_file():
            details = json.loads(result_file.read_text(encoding="utf-8")) if result_file.is_file() else {"error": f"worker exited {process.returncode}"}
            raise AppError("MAPPING_WORKER_FAILED", "The isolated mapping worker failed.", status_code=500, details=details, retryable=True)
        result = json.loads(result_file.read_text(encoding="utf-8"))
        if "error" in result:
            raise AppError("MAPPING_WORKER_FAILED", result["error"], status_code=500, details=result, retryable=True)
        return result["result"]

    def _run_export(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        spec = self._job_spec(job_id)
        project_id, session_id = job["project_id"], spec["session_id"]
        session = self.catalog.get_resource(project_id, "session", session_id)
        points = self.catalog.list_resources(project_id, "painted_point", parent_id=session_id, include_deleted=bool(spec.get("include_deleted")), limit=100000)
        paths = self.catalog.list_resources(project_id, "painted_path", parent_id=session_id, include_deleted=bool(spec.get("include_deleted")), limit=100000)
        export_dir = self.artifacts.project_path(project_id, Path("sessions") / session_id / "exports")
        export_dir.mkdir(parents=True, exist_ok=True)
        requested = spec.get("format", "json")
        normalized = "manifest" if requested == "session_manifest" else requested
        extension = "png" if normalized in {"screenshot", "point_overlay"} else "json" if normalized in {"json", "manifest"} else "csv"
        target = export_dir / f"{job['owner_id']}.{extension}"
        partial = target.with_suffix(target.suffix + ".partial")
        self._progress(job_id, "query", 1, 3, 1.0, f"Loaded {len(points)} points and {len(paths)} paths")
        if normalized == "csv":
            with partial.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["schema_version", "session_id", "record_type", "record_id", "sample_index", "timestamp", "x_m", "y_m", "z_m", "quality", "note", "deleted"])
                for item in points:
                    position = item.get("position_w_m", [None, None, None])
                    writer.writerow(["1.0.0", session_id, "point", item["point_id"], 0, item.get("timestamp"), *position, item.get("quality"), item.get("note", ""), item["deleted"]])
                for path in paths:
                    for index, sample in enumerate(path.get("samples", [])):
                        writer.writerow(["1.0.0", session_id, "path", path["path_id"], index, sample.get("timestamp"), *sample.get("position_w_m", [None, None, None]), sample.get("quality", path.get("quality")), path.get("note", ""), path["deleted"]])
        elif normalized in {"json", "manifest"}:
            document = {"schema_version": "1.0.0", "application_version": __version__, "session": session, "coordinate_frame": "W", "units": "m", "filters": spec.get("filters", {}), "points": points if normalized == "json" else [], "paths": paths if normalized == "json" else [], "checksums": {}}
            partial.write_text(json.dumps(document, indent=2, sort_keys=True, default=str), encoding="utf-8")
        elif normalized in {"screenshot", "point_overlay"}:
            partial.write_bytes(_paint_png(points, paths, overlay_only=normalized == "point_overlay"))
        else:
            raise AppError("EXPORT_FORMAT_UNSUPPORTED", "Supported v1 export formats are JSON, CSV, session manifest, screenshot, and point overlay.", status_code=422)
        self._progress(job_id, "write", 2, 3, 1.0, "Wrote export staging file")
        with partial.open("rb+") as handle:
            handle.flush(); os.fsync(handle.fileno())
        os.replace(partial, target)
        checksum = hashlib.sha256(target.read_bytes()).hexdigest()
        self._progress(job_id, "checksum", 3, 3, 1.0, "Verified export checksum")
        return {"format": requested, "relative_uri": target.relative_to(self.artifacts.root).as_posix(), "sha256": checksum, "checksum_sha256": checksum, "size_bytes": target.stat().st_size, "completed_at": utcnow().isoformat()}


def _paint_png(points: list[dict[str, Any]], paths: list[dict[str, Any]], *, overlay_only: bool) -> bytes:
    width, height = 1024, 768
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:] = [0, 0, 0] if overlay_only else [17, 24, 39]
    positions = [item.get("position_w_m") for item in points] + [sample.get("position_w_m") for path in paths for sample in path.get("samples", [])]
    positions = [value for value in positions if isinstance(value, list) and len(value) == 3]
    if positions:
        values = np.asarray(positions, dtype=float)
        low, high = values[:, :2].min(0), values[:, :2].max(0)
        span = np.maximum(high - low, 1e-6)
        pixels = ((values[:, :2] - low) / span * [width - 81, height - 81] + 40).astype(int)
        pixels[:, 1] = height - pixels[:, 1]
        for x, y in pixels:
            image[max(0, y - 3): min(height, y + 4), max(0, x - 3): min(width, x + 4)] = [39, 217, 171]
    return _png_rgb(image)


def _png_rgb(image: np.ndarray) -> bytes:
    rows = b"".join(b"\x00" + row.tobytes() for row in np.ascontiguousarray(image, dtype=np.uint8))
    height, width = image.shape[:2]
    def chunk(name: bytes, value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + name + value + struct.pack(">I", zlib.crc32(name + value) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b"")
