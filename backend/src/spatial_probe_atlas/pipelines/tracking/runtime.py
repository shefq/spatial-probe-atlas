from __future__ import annotations

from typing import Any

from sqlalchemy import select

from spatial_probe_atlas.adapters.persistence.database import ResourceRecord

from .indexed import IndexedCpuTrackingPipeline


_container: Any | None = None


def set_runtime_container(container: Any) -> None:
    global _container
    _container = container
    if not hasattr(container, "cpu_tracking_pipelines"):
        container.cpu_tracking_pipelines = {}


def real_tracking_frame(session_id: str) -> dict[str, Any] | None:
    container = _container
    if container is None or getattr(container.camera.adapter, "adapter_name", None) in {None, "replay"}:
        return None
    frame = container.camera.latest_frame
    if frame is None:
        return None
    with container.database.session() as db:
        session = db.get(ResourceRecord, session_id)
        if session is None or session.kind != "session":
            return None
        project_id = session.project_id
        session_payload = dict(session.payload or {})
    calibration = container.catalog.get_resource(project_id, "probe_calibration", session_payload["probe_calibration_id"])
    scene_map = container.catalog.get_resource(project_id, "scene_map", session_payload["map_id"])
    index = scene_map.get("localization_index")
    similarity = scene_map.get("similarity_s_w_m0")
    key = (session_id, calibration["revision"], scene_map["revision"], (index or {}).get("sha256"))
    pipeline = container.cpu_tracking_pipelines.get(key)
    if pipeline is None:
        pipeline = IndexedCpuTrackingPipeline(index or {}, similarity or {}, calibration, container.artifacts.root)
        container.cpu_tracking_pipelines.clear()
        container.cpu_tracking_pipelines[key] = pipeline
    return pipeline.track(session_id, frame)
