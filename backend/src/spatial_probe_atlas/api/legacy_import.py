"""Explicit prototype-project import API.

No endpoint scans for prototype projects.  A caller must supply one absolute directory and
the durable worker revalidates it after copying it into same-volume staging.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.domain.validation import validate_project_name
from spatial_probe_atlas.services.legacy_import import STAGE_COUNT, migration_report_path, validate_legacy_source


router = APIRouter()


class LegacyImportRequest(BaseModel):
    source_directory: str = Field(min_length=1, max_length=32767)
    project_name: str | None = Field(default=None, max_length=80)
    confirm_defaulted_probe_settings: bool = False


@router.post("/legacy-imports", status_code=202)
def create_legacy_import(
    request: Request,
    body: LegacyImportRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    container = request.app.state.container
    scope = "legacy-import:create"
    cached = container.catalog.idempotent_response(scope, idempotency_key)
    if cached is not None:
        return cached
    source = validate_legacy_source(Path(body.source_directory), container.settings.data_root)
    name = validate_project_name(body.project_name) if body.project_name else None
    free = shutil.disk_usage(container.settings.data_root).free
    if free <= container.settings.disk_reserve_bytes:
        raise AppError(
            "INSUFFICIENT_STORAGE",
            "The data root does not have the configured free-space reserve for a legacy import.",
            status_code=507,
            details={"free_bytes": free, "reserve_bytes": container.settings.disk_reserve_bytes},
        )
    target_project_id = str(uuid.uuid4())
    job = container.catalog.create_job(
        project_id=None,
        owner_id=target_project_id,
        type="legacy_import",
        spec={
            "stage_count": STAGE_COUNT,
            "project_id": target_project_id,
            "source_directory": str(source),
            "requested_project_name": name,
            "confirm_defaulted_probe_settings": body.confirm_defaulted_probe_settings,
        },
    )
    response = {**job, "target_project_id": target_project_id, "confirmation_recorded": body.confirm_defaulted_probe_settings}
    container.catalog.save_idempotent_response(scope, idempotency_key, response)
    container.jobs.submit(job["job_id"])
    return response


@router.get("/legacy-imports/{job_id}")
def get_legacy_import(request: Request, job_id: str) -> dict[str, Any]:
    job = request.app.state.container.catalog.get_job(job_id)
    if job["type"] != "legacy_import":
        raise AppError("LEGACY_IMPORT_NOT_FOUND", "The requested job is not a legacy import.", status_code=404)
    return {**job, "target_project_id": job.get("owner_id")}


@router.get("/legacy-imports/{job_id}/report")
def download_legacy_report(request: Request, job_id: str) -> FileResponse:
    container = request.app.state.container
    job = container.catalog.get_job(job_id)
    if job["type"] != "legacy_import":
        raise AppError("LEGACY_IMPORT_NOT_FOUND", "The requested job is not a legacy import.", status_code=404)
    if job["state"] != "completed":
        raise AppError("LEGACY_IMPORT_NOT_COMPLETE", "The migration report is available only after publication completes.", status_code=409)
    report = job.get("result", {}).get("report")
    project_id = job.get("project_id") or job.get("owner_id")
    if not isinstance(report, dict) or not project_id:
        raise AppError("LEGACY_MIGRATION_REPORT_MISSING", "The completed import has no migration report reference.", status_code=500)
    path = migration_report_path(container.artifacts, project_id, report["sha256"])
    return FileResponse(
        path,
        filename=f"spatial-probe-atlas-migration-{project_id}.json",
        media_type="application/json",
        headers={"ETag": f'"{report["sha256"]}"', "Cache-Control": "private, no-store"},
    )


__all__ = ["router"]
