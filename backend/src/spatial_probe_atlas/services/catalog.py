from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.adapters.persistence.database import (
    Database,
    IdempotencyRecord,
    JobRecord,
    ProjectRecord,
    ResourceRecord,
    ValidationRecord,
    job_dict,
    new_id,
    project_dict,
    resource_dict,
    utcnow,
)
from spatial_probe_atlas.domain.errors import AppError, not_found
from spatial_probe_atlas.domain.validation import validate_project_name


class Catalog:
    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        self.database = database
        self.artifacts = artifacts

    def create_project(self, name: str) -> dict[str, Any]:
        with self.database.session() as session:
            record = ProjectRecord(name=validate_project_name(name))
            session.add(record)
            session.flush()
            self.artifacts.project_dir(record.id)
            return project_dict(record)

    def get_project_record(self, session: Any, project_id: str) -> ProjectRecord:
        record = session.get(ProjectRecord, project_id)
        if record is None:
            raise not_found("project", project_id)
        return record

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            return project_dict(self.get_project_record(session, project_id))

    def list_projects(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.database.session() as session:
            query = select(ProjectRecord).order_by(ProjectRecord.updated_at.desc())
            if not include_archived:
                query = query.where(ProjectRecord.state != "archived")
            return [project_dict(row) for row in session.scalars(query)]

    def update_project(self, project_id: str, values: dict[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
        with self.database.session() as session:
            record = self.get_project_record(session, project_id)
            if expected_revision is not None and record.revision != expected_revision:
                raise AppError("ETAG_MISMATCH", "The project was changed by another request.", status_code=412)
            if "name" in values:
                record.name = validate_project_name(str(values["name"]))
            if "state" in values:
                record.state = str(values["state"])
            record.revision += 1
            record.updated_at = utcnow()
            session.flush()
            return project_dict(record)

    def set_project_state(self, project_id: str, state: str) -> dict[str, Any]:
        return self.update_project(project_id, {"state": state})

    def clone_project(self, project_id: str, name: str | None = None) -> dict[str, Any]:
        source = self.get_project(project_id)
        target_name = validate_project_name(name or f"{source['name']} copy")
        free = shutil.disk_usage(self.artifacts.root).free
        source_size = self.directory_size(self.artifacts.project_dir(project_id))
        if source_size + 10 * 1024**3 > free:
            raise AppError("INSUFFICIENT_STORAGE", "There is not enough free space to clone this project plus reserve.", status_code=507, details={"required_bytes": source_size + 10 * 1024**3, "free_bytes": free})
        with self.database.session() as session:
            target = ProjectRecord(name=target_name)
            session.add(target)
            session.flush()
            resources = list(session.scalars(select(ResourceRecord).where(ResourceRecord.project_id == project_id)))
            id_map: dict[str, str] = {row.id: new_id() for row in resources}
            for row in resources:
                session.add(ResourceRecord(
                    id=id_map[row.id], project_id=target.id, kind=row.kind,
                    parent_id=id_map.get(row.parent_id, row.parent_id), name=row.name, state=row.state,
                    payload=row.payload, revision=row.revision, deleted=row.deleted,
                ))
            target.active_map_id = id_map.get(source.get("active_map_id"))
            target.active_probe_calibration_id = id_map.get(source.get("active_probe_calibration_id"))
            target.active_registration_id = id_map.get(source.get("active_registration_id"))
            target.active_camera_calibration_id = id_map.get(source.get("active_camera_calibration_id"))
            self.artifacts.clone_project(project_id, target.id)
            session.flush()
            return project_dict(target)

    def create_resource(
        self, project_id: str, kind: str, *, state: str, payload: dict[str, Any] | None = None,
        parent_id: str | None = None, name: str | None = None, resource_id: str | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            self.get_project_record(session, project_id)
            record = ResourceRecord(id=resource_id or new_id(), project_id=project_id, kind=kind, parent_id=parent_id, name=name, state=state, payload=payload or {})
            session.add(record)
            session.flush()
            return resource_dict(record)

    def get_resource_record(self, session: Any, project_id: str, kind: str, resource_id: str) -> ResourceRecord:
        record = session.get(ResourceRecord, resource_id)
        if record is None or record.project_id != project_id or record.kind != kind:
            raise not_found(kind, resource_id)
        return record

    def get_resource(self, project_id: str, kind: str, resource_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            return resource_dict(self.get_resource_record(session, project_id, kind, resource_id))

    def list_resources(
        self, project_id: str, kind: str, *, parent_id: str | None = None, include_deleted: bool = False,
        limit: int = 100, cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            self.get_project_record(session, project_id)
            query = select(ResourceRecord).where(ResourceRecord.project_id == project_id, ResourceRecord.kind == kind)
            if parent_id is not None:
                query = query.where(ResourceRecord.parent_id == parent_id)
            if not include_deleted:
                query = query.where(ResourceRecord.deleted.is_(False))
            if cursor:
                query = query.where(ResourceRecord.id > cursor)
            query = query.order_by(ResourceRecord.id).limit(min(max(limit, 1), 1000))
            return [resource_dict(row) for row in session.scalars(query)]

    def update_resource(
        self, project_id: str, kind: str, resource_id: str, *, payload_patch: dict[str, Any] | None = None,
        state: str | None = None, deleted: bool | None = None, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            record = self.get_resource_record(session, project_id, kind, resource_id)
            if expected_revision is not None and record.revision != expected_revision:
                raise AppError("ETAG_MISMATCH", "The resource revision is stale.", status_code=412)
            if payload_patch:
                record.payload = {**(record.payload or {}), **payload_patch}
            if state is not None:
                record.state = state
            if deleted is not None:
                record.deleted = deleted
            record.revision += 1
            record.updated_at = utcnow()
            session.flush()
            return resource_dict(record)

    def delete_resource(self, project_id: str, kind: str, resource_id: str) -> dict[str, Any]:
        return self.update_resource(project_id, kind, resource_id, deleted=True)

    def activate(self, project_id: str, kind: str, resource_id: str) -> dict[str, Any]:
        column = {
            "scene_map": "active_map_id", "probe_calibration": "active_probe_calibration_id",
            "registration": "active_registration_id", "camera_calibration": "active_camera_calibration_id",
        }.get(kind)
        if column is None:
            raise ValueError(kind)
        with self.database.session() as session:
            project = self.get_project_record(session, project_id)
            record = self.get_resource_record(session, project_id, kind, resource_id)
            setattr(project, column, resource_id)
            project.revision += 1
            record.state = "active"
            record.revision += 1
            for sibling in session.scalars(select(ResourceRecord).where(ResourceRecord.project_id == project_id, ResourceRecord.kind == kind, ResourceRecord.id != resource_id, ResourceRecord.state == "active")):
                sibling.state = "superseded"
                sibling.revision += 1
            session.flush()
            return resource_dict(record)

    def project_summary(self, project_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        with self.database.session() as session:
            counts = dict(session.execute(select(ResourceRecord.kind, func.count()).where(ResourceRecord.project_id == project_id, ResourceRecord.deleted.is_(False)).group_by(ResourceRecord.kind)).all())
            active_jobs = list(session.scalars(select(JobRecord).where(JobRecord.project_id == project_id, JobRecord.state.in_(["queued", "admitted", "processing", "cancelling", "recoverable"]))))
        size = self.directory_size(self.artifacts.project_dir(project_id))
        return {
            **project, "size_bytes": size, "calculated_at": utcnow(), "counts": counts,
            "session_count": counts.get("session", 0), "frame_count": counts.get("capture_frame", 0),
            "map_count": counts.get("scene_map", 0), "active_jobs": [job_dict(job) for job in active_jobs],
            "readiness": {
                "map": bool(project["active_map_id"]), "probe_calibration": bool(project["active_probe_calibration_id"]),
                "registration": bool(project["active_registration_id"]),
                "live": all(project[key] for key in ("active_map_id", "active_probe_calibration_id", "active_registration_id")),
            },
        }

    @staticmethod
    def directory_size(path: Path) -> int:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

    def create_validation(self, project_id: str, kind: str, checksum: str, payload: dict[str, Any], summary: dict[str, Any], warnings: list[Any], errors: list[Any]) -> dict[str, Any]:
        with self.database.session() as session:
            self.get_project_record(session, project_id)
            record = ValidationRecord(project_id=project_id, kind=kind, valid=not errors, checksum=checksum, payload=payload, summary=summary, warnings=warnings, errors=errors, expires_at=utcnow() + timedelta(minutes=15))
            session.add(record)
            session.flush()
            return {"validation_id": record.id, "valid": record.valid, "schema_version": payload.get("schema_version"), "summary": summary, "warnings": warnings, "errors": errors, "expires_at": record.expires_at}

    def consume_validation(self, project_id: str, kind: str, validation_id: str) -> ValidationRecord:
        with self.database.session() as session:
            record = session.get(ValidationRecord, validation_id)
            if record is None or record.project_id != project_id or record.kind != kind:
                raise not_found("validation", validation_id)
            if not record.valid:
                raise AppError("VALIDATION_FAILED", "The staged file did not pass validation.", status_code=422, details={"errors": record.errors})
            expires = record.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= utcnow():
                raise AppError("VALIDATION_EXPIRED", "The staged validation has expired; validate the file again.", status_code=409)
            session.expunge(record)
            return record

    def create_job(self, *, project_id: str | None, owner_id: str | None, type: str, spec: dict[str, Any]) -> dict[str, Any]:
        with self.database.session() as session:
            if project_id:
                self.get_project_record(session, project_id)
            record = JobRecord(project_id=project_id, owner_id=owner_id, type=type, spec=spec, stage_count=int(spec.get("stage_count", 1)))
            session.add(record)
            session.flush()
            return job_dict(record)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                raise not_found("job", job_id)
            return job_dict(record)

    def list_jobs(self, project_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.session() as session:
            query = select(JobRecord)
            if project_id:
                query = query.where(JobRecord.project_id == project_id)
            return [job_dict(row) for row in session.scalars(query.order_by(JobRecord.created_at.desc()))]

    def idempotent_response(self, scope: str, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        with self.database.session() as session:
            row = session.get(IdempotencyRecord, {"key": key, "scope": scope})
            return row.response if row else None

    def save_idempotent_response(self, scope: str, key: str | None, response: dict[str, Any]) -> None:
        if not key:
            return
        with self.database.session() as session:
            session.merge(IdempotencyRecord(key=key, scope=scope, response=response))
