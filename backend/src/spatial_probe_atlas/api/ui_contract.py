from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from spatial_probe_atlas.api import system
from spatial_probe_atlas.domain.errors import AppError


router = APIRouter()


@router.get("/system/capabilities")
def flattened_capabilities(request: Request) -> dict[str, Any]:
    result = system._capabilities(request)
    compute = result["compute"]
    return {
        **result,
        "app_version": result["application_version"],
        "compute_state": compute["state"],
        "effective_compute_profile": compute["effective"],
        "gpu": compute.get("device"),
        "cuda_version": compute.get("torch_cuda_version"),
        "record3d_state": result["record3d"]["state"],
        "replay_available": True,
        "data_root": "<local-data-root>",
    }


@router.get("/system/resources")
def flattened_resources(request: Request) -> dict[str, Any]:
    container = request.app.state.container
    value = system._resources(request)
    memory, disk, process = value.get("memory", {}), value["disk"], value.get("process", {})
    warnings = [
        {
            **item,
            "id": item["code"].lower(),
            "message": item["code"].replace("_", " ").title(),
            "suggested_action": "Close other applications or free local disk space.",
        }
        for item in value["warnings"]
    ]
    project_size = sum(container.catalog.directory_size(container.artifacts.project_dir(item["project_id"])) for item in container.catalog.list_projects(include_archived=True))
    return {
        **value,
        "cpu_percent": process.get("cpu_percent"),
        "ram_used_percent": memory.get("percent"),
        "ram_total_bytes": memory.get("total_bytes"),
        "disk_free_bytes": disk["free_bytes"],
        "disk_total_bytes": disk["total_bytes"],
        "project_size_bytes": project_size,
        "vram_used_percent": None,
        "vram_total_bytes": None,
        "warnings": warnings,
    }


def _settings(request: Request) -> dict[str, Any]:
    raw = system._load_settings(request)
    return {
        "schema_version": 1,
        "display_units": raw.get("display_units", "mm"),
        "compute_profile": raw.get("compute_profile", request.app.state.container.settings.compute_profile),
        "point_budget": int(raw.get("point_budget", 3_000_000)),
        "decoded_cache_mib": int(raw.get("decoded_cache_mib", 512)),
        "continue_live_in_background": bool(raw.get("continue_live_in_background", False)),
        "log_level": str(raw.get("log_level", request.app.state.container.settings.log_level)).upper(),
    }


@router.get("/settings")
def complete_settings(request: Request) -> dict[str, Any]:
    return _settings(request)


@router.patch("/settings")
def patch_complete_settings(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    allowed = {"display_units", "compute_profile", "point_budget", "decoded_cache_mib", "continue_live_in_background", "log_level"}
    unknown = set(body) - allowed
    if unknown:
        raise AppError("SETTINGS_FIELD_UNKNOWN", "One or more settings fields are not supported.", status_code=422, details={"fields": sorted(unknown)})
    value = {**_settings(request), **body, "schema_version": 1}
    if value["display_units"] not in {"m", "mm"} or value["compute_profile"] not in {"auto", "cpu", "cuda"}:
        raise AppError("SETTINGS_VALUE_INVALID", "Display units or compute profile is invalid.", status_code=422)
    if not 500_000 <= int(value["point_budget"]) <= 10_000_000 or not 128 <= int(value["decoded_cache_mib"]) <= 4096:
        raise AppError("SETTINGS_VALUE_INVALID", "Point and cache budgets are outside supported limits.", status_code=422)
    if not isinstance(value["continue_live_in_background"], bool) or str(value["log_level"]).upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise AppError("SETTINGS_VALUE_INVALID", "Background-live or log-level setting is invalid.", status_code=422)
    value["point_budget"] = int(value["point_budget"])
    value["decoded_cache_mib"] = int(value["decoded_cache_mib"])
    value["log_level"] = str(value["log_level"]).upper()
    request.app.state.container.artifacts.atomic_write_json(system._settings_file(request), value)
    value["restart_required"] = "compute_profile" in body or "log_level" in body
    return value
