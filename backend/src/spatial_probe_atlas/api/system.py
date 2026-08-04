from __future__ import annotations

import importlib.util
import json
import os
import platform
import secrets
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from spatial_probe_atlas import __version__
from spatial_probe_atlas.api.schemas import SettingsPatch
from spatial_probe_atlas.compute.cuda import probe_cuda
from spatial_probe_atlas.compute.profiles import CPU_MAPPING_PROFILE, CUDA_MAPPING_PROFILE
from spatial_probe_atlas.domain.errors import AppError


router = APIRouter()
health_router = APIRouter()


def _capabilities(request: Request) -> dict[str, Any]:
    container = request.app.state.container
    record3d = container.camera.adapters["record3d"]
    cuda = probe_cuda(container.settings.data_root / "models")
    effective = (
        CUDA_MAPPING_PROFILE
        if container.settings.compute_profile in {"auto", "cuda"} and cuda.available
        else CPU_MAPPING_PROFILE
    )
    return {
        "application_version": __version__, "api_version": "v1", "schema_version": "1.0.0",
        "python": platform.python_version(), "platform": platform.platform(), "compute": {**cuda.as_dict(), "configured": container.settings.compute_profile, "effective": effective},
        "record3d": {"state": "available" if record3d.available else "not_available", "sdk_version": "1.4.1" if record3d.available else None},
        "opencv": {"state": "available" if importlib.util.find_spec("cv2") else "not_available"},
        "pycolmap": {"state": "available" if importlib.util.find_spec("pycolmap") else "not_available", "required_for_replay": False},
        "replay": {"state": "available", "device_id": "replay:synthetic"},
    }


def _resources(request: Request) -> dict[str, Any]:
    root = request.app.state.container.settings.data_root
    disk = shutil.disk_usage(root)
    result: dict[str, Any] = {"disk": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free}, "warnings": []}
    try:
        import psutil
        memory = psutil.virtual_memory()
        process = psutil.Process()
        result.update({"memory": {"total_bytes": memory.total, "available_bytes": memory.available, "percent": memory.percent}, "process": {"rss_bytes": process.memory_info().rss, "cpu_percent": process.cpu_percent(), "threads": process.num_threads()}})
        if memory.percent > 92:
            result["warnings"].append({"code": "RAM_CRITICAL", "severity": "error"})
        elif memory.percent > 70:
            result["warnings"].append({"code": "RAM_HIGH", "severity": "warning"})
    except Exception:
        result["memory"] = {"state": "not_available"}
    if disk.free < 20 * 1024**3:
        result["warnings"].append({"code": "DISK_LOW", "severity": "warning", "free_bytes": disk.free})
    result["calculated_at"] = datetime.now(UTC).isoformat()
    return result


@health_router.get("/health/live")
def live() -> dict[str, Any]:
    return {"status": "ok", "application": "spatial-probe-atlas", "version": __version__}


@health_router.get("/health/ready")
def ready(request: Request) -> dict[str, Any]:
    container = request.app.state.container
    integrity = container.database.integrity_check()
    status = "ready" if integrity == "ok" else "degraded"
    return {"status": status, "database": integrity, "data_root_writable": os.access(container.settings.data_root, os.W_OK), "frontend_built": bool(container.settings.frontend_dist and container.settings.frontend_dist.joinpath("index.html").is_file()), "replay_available": True}


@health_router.get("/bootstrap")
def bootstrap(request: Request, token: str | None = None) -> Response:
    container = request.app.state.container
    container.bootstrap_consumed = True
    response = RedirectResponse(url="/projects", status_code=303)
    response.set_cookie("spa_session", container.session_secret, httponly=True, samesite="lax", secure=False, path="/")
    return response


@router.get("/health/live")
def api_live() -> dict[str, Any]:
    return live()


@router.get("/health/ready")
def api_ready(request: Request) -> dict[str, Any]:
    return ready(request)


@router.get("/system/capabilities")
def capabilities(request: Request) -> dict[str, Any]:
    return _capabilities(request)


@router.get("/system/resources")
def resources(request: Request) -> dict[str, Any]:
    return _resources(request)


@router.post("/system/diagnostics")
def diagnostics(request: Request) -> dict[str, Any]:
    container = request.app.state.container
    checks = [
        {"name": "database_integrity", "status": "PASS" if container.database.integrity_check() == "ok" else "FAIL", "impact": "Durable metadata"},
        {"name": "data_root_write", "status": "PASS" if os.access(container.settings.data_root, os.W_OK) else "FAIL", "impact": "Capture and exports"},
        {"name": "replay_camera", "status": "PASS", "impact": "Hardware-free validation"},
        {"name": "record3d_sdk", "status": "PASS" if container.camera.adapters["record3d"].available else "WARN", "impact": "Real iPhone capture unavailable; replay remains functional", "fix": "Install record3d==1.4.1 for Python 3.11"},
    ]
    return {"run_id": secrets.token_hex(8), "status": "PASS" if all(item["status"] != "FAIL" for item in checks) else "FAIL", "checks": checks, "capabilities": _capabilities(request), "resources": _resources(request), "database_integrity": container.database.integrity_check()}


def _settings_file(request: Request) -> Path:
    return request.app.state.container.settings.data_root / "settings.json"


def _load_settings(request: Request) -> dict[str, Any]:
    defaults = {"schema_version": 1, "display_units": "mm", "compute_profile": request.app.state.container.settings.compute_profile, "point_budget": 3000000, "decoded_cache_mib": 512}
    path = _settings_file(request)
    if path.is_file():
        try:
            defaults.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            defaults["warning"] = "settings_file_invalid"
    return defaults


@router.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    return _load_settings(request)


@router.patch("/settings")
def patch_settings(request: Request, body: SettingsPatch) -> dict[str, Any]:
    container = request.app.state.container
    values = {**_load_settings(request), **body.model_dump(exclude_none=True), "schema_version": 1}
    container.artifacts.atomic_write_json(_settings_file(request), values)
    values["restart_required"] = "compute_profile" in body.model_fields_set
    return values


@router.post("/support-bundles", status_code=202)
def support_bundle(request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
    container = request.app.state.container
    if bool((body or {}).get("include_raw_frames")):
        raise AppError(
            "RAW_FRAMES_NOT_SUPPORTED",
            "V1 support bundles never include raw frames.",
            status_code=422,
        )
    job = container.catalog.create_job(
        project_id=None,
        owner_id=None,
        type="support_bundle",
        spec={"schema_version": 1, "stage_count": 4, "include_raw_frames": False},
    )
    container.jobs.submit(job["job_id"])
    return job


@router.post("/system/repair-reindex", status_code=202)
def repair_reindex(request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
    container = request.app.state.container
    if set((body or {}).keys()) - {"mode"} or (body or {}).get("mode", "non_destructive_candidate") != "non_destructive_candidate":
        raise AppError("REPAIR_MODE_UNSUPPORTED", "V1 repair creates a non-destructive reindexed candidate only.", status_code=422)
    job = container.catalog.create_job(project_id=None, owner_id=None, type="repair_reindex", spec={"schema_version": 1, "stage_count": 4, "mode": "non_destructive_candidate"})
    container.jobs.submit(job["job_id"])
    return job
