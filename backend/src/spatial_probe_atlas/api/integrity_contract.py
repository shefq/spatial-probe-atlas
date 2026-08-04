from __future__ import annotations

import hashlib
import json
import math
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, Header, Request
from sqlalchemy import select

from spatial_probe_atlas.adapters.persistence.database import ResourceRecord, utcnow
from spatial_probe_atlas.api import sessions_review
from spatial_probe_atlas.api.schemas import SessionCreate
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.observability import read_structured_log_tail
from spatial_probe_atlas.services.clone import clone_project_exact


router = APIRouter()


def _canonical_checksum(value: dict[str, Any]) -> str:
    portable = {key: item for key, item in value.items() if key not in {"artifact", "checksum"}}
    return hashlib.sha256(json.dumps(portable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _repair_calibration_checksums(container: Any, project_id: str) -> None:
    with container.database.session() as db:
        rows = list(
            db.scalars(
                select(ResourceRecord).where(
                    ResourceRecord.project_id == project_id,
                    ResourceRecord.kind.in_(["probe_calibration", "camera_calibration"]),
                )
            )
        )
        for row in rows:
            payload = dict(row.payload or {})
            payload["checksum"] = _canonical_checksum(payload)
            row.payload = payload
            row.updated_at = utcnow()


@router.post("/projects/{project_id}/clone", status_code=201)
def clone_project_with_integrity(request: Request, project_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    container = request.app.state.container
    project = clone_project_exact(container.catalog, project_id, (body or {}).get("name"))
    _repair_calibration_checksums(container, project["project_id"])
    return {"project": project}


def _similarity_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        scale = float(value["scale"])
        rotation = np.asarray(value["rotation"], dtype=float).reshape(3, 3)
        translation = np.asarray(value["translation"], dtype=float).reshape(3)
    except (KeyError, TypeError, ValueError):
        return False
    return math.isfinite(scale) and scale > 0 and np.isfinite(rotation).all() and np.isfinite(translation).all()


def _strict_session_preflight(container: Any, project_id: str, session: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    project = container.catalog.get_project(project_id)
    scene_map = probe = registration = None
    try:
        if project.get("active_map_id"):
            scene_map = container.catalog.get_resource(project_id, "scene_map", project["active_map_id"])
        if project.get("active_probe_calibration_id"):
            probe = container.catalog.get_resource(project_id, "probe_calibration", project["active_probe_calibration_id"])
        if project.get("active_registration_id"):
            registration = container.catalog.get_resource(project_id, "registration", project["active_registration_id"])
    except AppError:
        pass
    exact_binding = bool(
        registration
        and registration.get("map_id") == project.get("active_map_id")
        and registration.get("probe_calibration_id") == project.get("active_probe_calibration_id")
    )
    registration_valid = bool(
        registration
        and registration.get("state") == "active"
        and registration.get("validation_status") in {"passed", "accepted_with_warning"}
        and _similarity_valid(registration.get("similarity_s_w_m0"))
    )
    free = shutil.disk_usage(container.settings.data_root).free
    checks = [
        {"key": "camera", "label": "Camera ready", "passed": container.camera.project_id == project_id and container.camera.state == "ready", "detail": container.camera.state, "required_route": f"/projects/{project_id}/camera"},
        {"key": "map", "label": "Metric map active", "passed": bool(scene_map and scene_map.get("state") == "ready_metric"), "required_route": f"/projects/{project_id}/mapping"},
        {"key": "probe", "label": "Probe calibration active", "passed": bool(probe and probe.get("state") == "active"), "required_route": f"/projects/{project_id}/registration"},
        {"key": "registration", "label": "Metric registration active", "passed": registration_valid, "required_route": f"/projects/{project_id}/registration"},
        {"key": "dependency_binding", "label": "Registration matches active map and probe revisions", "passed": exact_binding, "required_route": f"/projects/{project_id}/registration"},
        {"key": "storage", "label": "Storage reserve available", "passed": free > container.settings.disk_reserve_bytes, "detail": f"{free} bytes free"},
    ]
    if session:
        checks.extend(
            [
                {"key": "map_revision", "label": "Session map revision unchanged", "passed": session.get("map_id") == project.get("active_map_id")},
                {"key": "calibration_revision", "label": "Session probe revision unchanged", "passed": session.get("probe_calibration_id") == project.get("active_probe_calibration_id")},
                {"key": "registration_revision", "label": "Session registration revision unchanged", "passed": session.get("registration_id") == project.get("active_registration_id")},
            ]
        )
    return checks


# Existing lifecycle functions resolve this module global at call time, so one strict
# implementation covers both native and browser-compatible route surfaces.
sessions_review._session_preflight = _strict_session_preflight


@router.post("/projects/{project_id}/sessions", status_code=201)
def create_session_with_dependency_preflight(
    request: Request,
    project_id: str,
    body: SessionCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    checks = _strict_session_preflight(request.app.state.container, project_id)
    dependency_keys = {"map", "probe", "registration", "dependency_binding"}
    failures = [item for item in checks if item["key"] in dependency_keys and not item["passed"]]
    if failures:
        raise AppError("SESSION_DEPENDENCY_PREFLIGHT_FAILED", "Create a session only after an exact active metric map, probe calibration, and registration are bound.", status_code=409, details={"checks": checks})
    return sessions_review.create_session(request, project_id, body, idempotency_key)


@router.post("/projects/{project_id}/registrations/{registration_id}/activate")
def activate_registration_strict(request: Request, project_id: str, registration_id: str) -> dict[str, Any]:
    container = request.app.state.container
    registration = container.catalog.get_resource(project_id, "registration", registration_id)
    project = container.catalog.get_project(project_id)
    if registration.get("state") != "validated" or registration.get("validation_status") not in {"passed", "accepted_with_warning"}:
        raise AppError("REGISTRATION_NOT_VALIDATED", "Registration must be in the validated state before activation.", status_code=409)
    if registration.get("map_id") != project.get("active_map_id"):
        raise AppError("REGISTRATION_MAP_MISMATCH", "Registration does not reference the exact active map revision.", status_code=409)
    if registration.get("probe_calibration_id") != project.get("active_probe_calibration_id"):
        raise AppError("REGISTRATION_PROBE_MISMATCH", "Registration does not reference the exact active probe calibration revision.", status_code=409)
    similarity = registration.get("similarity_s_w_m0")
    if not _similarity_valid(similarity):
        raise AppError("REGISTRATION_SIMILARITY_INVALID", "Registration similarity must have a finite positive scale, rotation, and translation.", status_code=422)
    scene_map = container.catalog.get_resource(project_id, "scene_map", registration["map_id"])
    if scene_map.get("state") not in {"active", "ready_unscaled", "ready_metric"}:
        raise AppError("REGISTRATION_MAP_NOT_READY", "The active map revision is not a validated published reconstruction.", status_code=409)
    result = container.catalog.activate(project_id, "registration", registration_id)
    binding = {
        "registration_id": registration_id,
        "source_frame": "M0",
        "destination_frame": "W",
        "source_units": "arbitrary",
        "destination_units": "m",
        "convention_version": "1.0.0",
        "similarity_s_w_m0": similarity,
        "bound_at": datetime.now(UTC).isoformat(),
    }
    container.catalog.update_resource(
        project_id,
        "scene_map",
        registration["map_id"],
        state="ready_metric",
        payload_patch={
            "raw_coordinate_frame": "M0",
            "raw_units": "arbitrary",
            "published_coordinate_frame": "W",
            "published_units": "m",
            "similarity_s_w_m0": similarity,
            "metric_binding": binding,
        },
    )
    return {**result, "active": True, "metric_binding": binding}


@router.get("/projects/{project_id}/maps/{map_id}/point-cloud/manifest")
def map_manifest_with_metric_binding(request: Request, project_id: str, map_id: str) -> dict[str, Any]:
    container = request.app.state.container
    scene_map = container.catalog.get_resource(project_id, "scene_map", map_id)
    info = scene_map.get("manifest")
    if not isinstance(info, dict):
        raise AppError("MAP_ARTIFACT_NOT_READY", "The map manifest has not been published.", status_code=409)
    path = container.artifacts.root / info["relative_uri"]
    if not path.is_file() or container.artifacts.sha256(path) != info.get("sha256"):
        raise AppError("MAP_ARTIFACT_CORRUPT", "The map manifest is missing or failed its checksum.", status_code=500)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if scene_map.get("metric_binding"):
        manifest["metric_binding"] = scene_map["metric_binding"]
        manifest["published_coordinate_frame"] = "W"
        manifest["published_units"] = "m"
    return manifest


@router.get("/system/logs/tail")
def structured_log_tail(request: Request, limit: int = 100, lines: int | None = None) -> dict[str, Any]:
    root = request.app.state.container.settings.data_root
    count = min(max(int(lines if lines is not None else limit), 1), 1000)
    return {"items": read_structured_log_tail(root / "logs", limit=count, data_root=root), "redacted": True}
