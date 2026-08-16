from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.observability import read_structured_log_tail
from spatial_probe_atlas.services.clone import clone_project_exact


router = APIRouter()


@router.post("/projects/{project_id}/clone", status_code=201)
def clone_project_contract(request: Request, project_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"project": clone_project_exact(request.app.state.container.catalog, project_id, (body or {}).get("name"))}


@router.get("/projects/{project_id}/maps/{map_id}/point-cloud/manifest")
def native_map_manifest(request: Request, project_id: str, map_id: str) -> dict[str, Any]:
    from . import projects_mapping
    from fastapi import Response
    return projects_mapping.map_manifest(request, project_id, map_id, Response())


@router.get("/system/logs/tail")
def log_tail(request: Request, lines: int = 200) -> dict[str, Any]:
    root = request.app.state.container.settings.data_root
    count = min(max(lines, 1), 1000)
    return {"items": read_structured_log_tail(root / "logs", limit=count, data_root=root), "redacted": True}


@router.post("/system/logs/reveal")
def reveal_logs(request: Request) -> dict[str, Any]:
    if os.name != "nt":
        raise AppError("REVEAL_NOT_AVAILABLE", "Reveal logs is available only on Windows.", status_code=503)
    path = request.app.state.container.settings.data_root / "logs"
    subprocess.Popen(["explorer.exe", str(path)], close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"revealed": True}


def _is_link(path: Path) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(value, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & 0x400)


def _is_fixed_local_path(path: Path) -> bool:
    if os.name != "nt":
        return True
    if str(path).startswith("\\\\"):
        return False
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor))) == 3
    except Exception:
        return False


def _atomic_destination_probe(directory: Path) -> None:
    probe_id = uuid.uuid4().hex
    partial = directory / f".spa-write-test-{probe_id}.partial"
    published = directory / f".spa-write-test-{probe_id}"
    try:
        with partial.open("xb") as handle:
            handle.write(b"spatial-probe-atlas")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, published)
    finally:
        partial.unlink(missing_ok=True)
        published.unlink(missing_ok=True)


@router.post("/system/data-root-migrations", status_code=202)
def migrate_data_root(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    container = request.app.state.container
    destination_text = str(body.get("destination") or "").strip()
    requested = Path(destination_text).expanduser()
    if not destination_text or not requested.is_absolute():
        raise AppError("DATA_ROOT_DESTINATION_INVALID", "Choose an absolute local destination directory.", status_code=422)
    if _is_link(requested):
        raise AppError("DATA_ROOT_DESTINATION_LINK_UNSAFE", "The destination may not be a filesystem link.", status_code=422)
    destination = requested.resolve(strict=False)
    source = container.settings.data_root.resolve()
    target = (destination / "SpatialProbeAtlas").resolve(strict=False)
    if (target == source or source in target.parents or target in source.parents
            or destination == source or source in destination.parents or destination in source.parents):
        raise AppError("DATA_ROOT_DESTINATION_OVERLAP", "Source and destination data roots may not overlap.", status_code=422)
    if not _is_fixed_local_path(target):
        raise AppError("DATA_ROOT_DESTINATION_NOT_LOCAL", "V1 data-root migration supports fixed local drives only.", status_code=422)
    if target.exists() or _is_link(target):
        raise AppError("DATA_ROOT_DESTINATION_NOT_EMPTY", "The destination SpatialProbeAtlas directory already exists.", status_code=409)
    running_sessions = [item for project in container.catalog.list_projects(include_archived=True) for item in container.catalog.list_resources(project["id"], "session", limit=1000) if item["state"] in {"running", "paused", "degraded", "stopping"}]
    if running_sessions or any(job["state"] in {"admitted", "processing", "cancelling"} for job in container.catalog.list_jobs()):
        raise AppError("DATA_ROOT_MIGRATION_BUSY", "Stop live sessions and active jobs before migration.", status_code=423)
    required = container.catalog.directory_size(source) + container.settings.disk_reserve_bytes
    destination.mkdir(parents=True, exist_ok=True)
    if _is_link(destination):
        raise AppError("DATA_ROOT_DESTINATION_LINK_UNSAFE", "The destination may not be a filesystem link.", status_code=422)
    if shutil.disk_usage(destination).free < required:
        raise AppError("INSUFFICIENT_STORAGE", "Destination lacks migration size plus reserve.", status_code=507)
    try:
        _atomic_destination_probe(destination)
    except OSError as exc:
        raise AppError("DATA_ROOT_DESTINATION_NOT_WRITABLE", "The destination failed an atomic-write test.", status_code=422, details={"reason": str(exc)}) from exc
    job = container.catalog.create_job(
        project_id=None,
        owner_id=None,
        type="data_root_migration",
        spec={
            "schema_version": 1,
            "stage_count": 4,
            "destination_root": str(target),
            "disk_reserve_bytes": container.settings.disk_reserve_bytes,
        },
    )
    container.jobs.submit(job["job_id"])
    return job
