"""Create the v1 local catalog schema.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    existing = set(inspect(op.get_bind()).get_table_names())

    if "projects" not in existing:
        op.create_table(
            "projects",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("active_map_id", sa.String(length=36), nullable=True),
            sa.Column("active_probe_calibration_id", sa.String(length=36), nullable=True),
            sa.Column("active_registration_id", sa.String(length=36), nullable=True),
            sa.Column("active_camera_calibration_id", sa.String(length=36), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_projects_state"), "projects", ["state"], unique=False)

    if "resources" not in existing:
        op.create_table(
            "resources",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=40), nullable=False),
            sa.Column("parent_id", sa.String(length=36), nullable=True),
            sa.Column("name", sa.String(length=120), nullable=True),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("deleted", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_resources_deleted"), "resources", ["deleted"], unique=False)
        op.create_index(op.f("ix_resources_kind"), "resources", ["kind"], unique=False)
        op.create_index(op.f("ix_resources_parent_id"), "resources", ["parent_id"], unique=False)
        op.create_index(op.f("ix_resources_project_id"), "resources", ["project_id"], unique=False)
        op.create_index(op.f("ix_resources_state"), "resources", ["state"], unique=False)
        op.create_index("ix_resource_scope", "resources", ["project_id", "kind", "parent_id"], unique=False)

    if "jobs" not in existing:
        op.create_table(
            "jobs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=True),
            sa.Column("owner_id", sa.String(length=36), nullable=True),
            sa.Column("type", sa.String(length=40), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("stage", sa.String(length=64), nullable=False),
            sa.Column("stage_index", sa.Integer(), nullable=False),
            sa.Column("stage_count", sa.Integer(), nullable=False),
            sa.Column("progress", sa.Float(), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("warnings", sa.JSON(), nullable=False),
            sa.Column("spec", sa.JSON(), nullable=False),
            sa.Column("checkpoint", sa.JSON(), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("error", sa.JSON(), nullable=True),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_jobs_owner_id"), "jobs", ["owner_id"], unique=False)
        op.create_index(op.f("ix_jobs_project_id"), "jobs", ["project_id"], unique=False)
        op.create_index(op.f("ix_jobs_state"), "jobs", ["state"], unique=False)
        op.create_index(op.f("ix_jobs_type"), "jobs", ["type"], unique=False)

    if "validations" not in existing:
        op.create_table(
            "validations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=40), nullable=False),
            sa.Column("valid", sa.Boolean(), nullable=False),
            sa.Column("checksum", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("summary", sa.JSON(), nullable=False),
            sa.Column("warnings", sa.JSON(), nullable=False),
            sa.Column("errors", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_validations_project_id"), "validations", ["project_id"], unique=False)

    if "idempotency" not in existing:
        op.create_table(
            "idempotency",
            sa.Column("key", sa.String(length=160), nullable=False),
            sa.Column("scope", sa.String(length=160), nullable=False),
            sa.Column("response", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("key", "scope"),
        )


def downgrade() -> None:
    for table in ("idempotency", "validations", "jobs", "resources", "projects"):
        op.drop_table(table)
