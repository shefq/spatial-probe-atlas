from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class ProjectRecord(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="active", index=True)
    active_map_id: Mapped[str | None] = mapped_column(String(36))
    active_probe_calibration_id: Mapped[str | None] = mapped_column(String(36))
    active_registration_id: Mapped[str | None] = mapped_column(String(36))
    active_camera_calibration_id: Mapped[str | None] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ResourceRecord(Base):
    __tablename__ = "resources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (Index("ix_resource_scope", "project_id", "kind", "parent_id"),)


class JobRecord(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(36), index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    stage_index: Mapped[int] = mapped_column(Integer, default=0)
    stage_count: Mapped[int] = mapped_column(Integer, default=1)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="Queued")
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ValidationRecord(Base):
    __tablename__ = "validations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    valid: Mapped[bool] = mapped_column(Boolean)
    checksum: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IdempotencyRecord(Base):
    __tablename__ = "idempotency"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    scope: Mapped[str] = mapped_column(String(160), primary_key=True)
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
        event.listen(self.engine, "connect", self._configure_sqlite)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    @staticmethod
    def _configure_sqlite(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def migrate(self) -> None:
        from spatial_probe_atlas.migration_runtime import upgrade_database

        upgrade_database(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.sessions() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def integrity_check(self) -> str:
        with self.engine.connect() as connection:
            return str(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one())

    def backup(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = self.engine.raw_connection()
        try:
            import sqlite3
            output = sqlite3.connect(target)
            try:
                raw.driver_connection.backup(output)
            finally:
                output.close()
        finally:
            raw.close()


def project_dict(record: ProjectRecord) -> dict[str, Any]:
    return {
        "project_id": record.id, "id": record.id, "name": record.name, "state": record.state,
        "active_map_id": record.active_map_id, "active_probe_calibration_id": record.active_probe_calibration_id,
        "active_registration_id": record.active_registration_id, "active_camera_calibration_id": record.active_camera_calibration_id,
        "revision": record.revision, "created_at": record.created_at, "updated_at": record.updated_at,
    }


def resource_dict(record: ResourceRecord) -> dict[str, Any]:
    key = {"capture_set": "capture_set_id", "capture_frame": "frame_id", "scene_map": "map_id", "camera_calibration": "camera_calibration_id", "probe_calibration": "probe_calibration_id", "registration": "registration_id", "session": "session_id", "painted_point": "point_id", "painted_path": "path_id", "export": "export_id"}.get(record.kind, "id")
    result = dict(record.payload or {})
    result.update({key: record.id, "id": record.id, "project_id": record.project_id, "kind": record.kind, "parent_id": record.parent_id, "name": record.name, "state": record.state, "revision": record.revision, "deleted": record.deleted, "created_at": record.created_at, "updated_at": record.updated_at})
    return result


def job_dict(record: JobRecord) -> dict[str, Any]:
    return {
        "job_id": record.id, "id": record.id, "project_id": record.project_id, "owner_id": record.owner_id, "type": record.type,
        "state": record.state, "stage": record.stage, "stage_index": record.stage_index, "stage_count": record.stage_count,
        "progress": record.progress, "message": record.message, "warnings": record.warnings, "checkpoint": record.checkpoint,
        "result": record.result, "error": record.error, "cancel_requested": record.cancel_requested, "attempt": record.attempt,
        "heartbeat_at": record.heartbeat_at, "created_at": record.created_at, "updated_at": record.updated_at,
        "started_at": record.started_at, "finished_at": record.finished_at,
    }
