from __future__ import annotations

from typing import Any

from sqlalchemy import select

from spatial_probe_atlas.adapters.persistence.database import ResourceRecord, resource_dict, utcnow
from spatial_probe_atlas.domain.errors import AppError

from .catalog import Catalog as BaseCatalog


class Catalog(BaseCatalog):
    """Catalog variant that makes active dependency changes atomic and invalidating."""

    def activate(self, project_id: str, kind: str, resource_id: str) -> dict[str, Any]:
        column = {
            "scene_map": "active_map_id",
            "probe_calibration": "active_probe_calibration_id",
            "registration": "active_registration_id",
            "camera_calibration": "active_camera_calibration_id",
        }.get(kind)
        if column is None:
            raise ValueError(kind)
        with self.database.session() as session:
            project = self.get_project_record(session, project_id)
            record = self.get_resource_record(session, project_id, kind, resource_id)
            if record.deleted:
                raise AppError("RESOURCE_DELETED", "A deleted revision cannot be activated.", status_code=409)

            if kind in {"scene_map", "probe_calibration"} and project.active_registration_id:
                registration = session.get(ResourceRecord, project.active_registration_id)
                if registration is not None and registration.project_id == project_id:
                    payload = dict(registration.payload or {})
                    if payload.get("is_aruco_mode") or kind == "probe_calibration":
                        if kind == "probe_calibration":
                            payload["probe_calibration_id"] = resource_id
                        if kind == "scene_map":
                            payload["map_id"] = resource_id
                        registration.payload = payload
                        registration.revision += 1
                        registration.updated_at = utcnow()
                    else:
                        registration.state = "superseded"
                        registration.revision += 1
                        registration.updated_at = utcnow()
                        project.active_registration_id = None

            setattr(project, column, resource_id)
            project.revision += 1
            project.updated_at = utcnow()
            record.state = "active"
            record.revision += 1
            record.updated_at = utcnow()
            for sibling in session.scalars(
                select(ResourceRecord).where(
                    ResourceRecord.project_id == project_id,
                    ResourceRecord.kind == kind,
                    ResourceRecord.id != resource_id,
                    ResourceRecord.state == "active",
                )
            ):
                sibling.state = "superseded"
                sibling.revision += 1
                sibling.updated_at = utcnow()
            session.flush()
            return resource_dict(record)
