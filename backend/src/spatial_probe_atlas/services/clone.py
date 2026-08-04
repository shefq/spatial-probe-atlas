from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import select

from spatial_probe_atlas.adapters.persistence.database import ProjectRecord, ResourceRecord, new_id, project_dict
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.domain.validation import validate_project_name


def clone_project_exact(catalog: Any, project_id: str, name: str | None) -> dict[str, Any]:
    """Clone a project while preserving the invariant that every artifact URI resolves.

    Resource identifiers are embedded in both database payloads and artifact paths.  The
    clone therefore renames the copied path components before rewriting manifests and
    payloads, then recomputes and verifies every referenced artifact checksum in the same
    database transaction.
    """
    source = catalog.get_project(project_id)
    source_dir = catalog.artifacts.project_dir(project_id)
    source_size = catalog.directory_size(source_dir)
    free = shutil.disk_usage(catalog.artifacts.root).free
    if source_size + 10 * 1024**3 > free:
        raise AppError(
            "INSUFFICIENT_STORAGE",
            "There is not enough free space to clone this project plus reserve.",
            status_code=507,
        )

    target_id = new_id()
    target_dir = catalog.artifacts.projects / target_id
    with catalog.database.session() as session:
        resources = list(session.scalars(select(ResourceRecord).where(ResourceRecord.project_id == project_id)))
        id_map = {record.id: new_id() for record in resources}
        project = ProjectRecord(id=target_id, name=validate_project_name(name or f"{source['name']} copy"))
        session.add(project)
        session.flush()
        try:
            shutil.copytree(source_dir, target_dir)
            _rename_artifact_paths(target_dir, id_map)
            _rewrite_json_artifacts(target_dir, project_id, target_id, id_map)
            for record in resources:
                payload = _rewrite_value(record.payload, project_id, target_id, id_map)
                _refresh_and_verify_artifact_metadata(payload, catalog.artifacts.root)
                session.add(
                    ResourceRecord(
                        id=id_map[record.id],
                        project_id=target_id,
                        kind=record.kind,
                        parent_id=id_map.get(record.parent_id, record.parent_id),
                        name=record.name,
                        state=record.state,
                        payload=payload,
                        revision=record.revision,
                        deleted=record.deleted,
                    )
                )
            project.active_map_id = id_map.get(source.get("active_map_id"))
            project.active_probe_calibration_id = id_map.get(source.get("active_probe_calibration_id"))
            project.active_registration_id = id_map.get(source.get("active_registration_id"))
            project.active_camera_calibration_id = id_map.get(source.get("active_camera_calibration_id"))
            session.flush()
            return project_dict(project)
        except Exception:
            if target_dir.exists():
                shutil.rmtree(target_dir)
            raise


def _replace_ids(text: str, id_map: dict[str, str]) -> str:
    result = text
    for old, new in id_map.items():
        result = result.replace(old, new)
    return result


def _rename_artifact_paths(target_dir: Path, id_map: dict[str, str]) -> None:
    """Rename identifier-bearing copied paths from leaves toward the project root."""
    paths = sorted(target_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for path in paths:
        new_name = _replace_ids(path.name, id_map)
        if new_name == path.name:
            continue
        destination = path.with_name(new_name)
        if destination.exists():
            raise AppError(
                "CLONE_ARTIFACT_PATH_CONFLICT",
                "Cloned artifact path rewriting produced a collision.",
                status_code=409,
                details={"path": destination.relative_to(target_dir).as_posix()},
            )
        path.rename(destination)


def _rewrite_value(value: Any, source_id: str, target_id: str, id_map: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite_value(item, source_id, target_id, id_map) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_value(item, source_id, target_id, id_map) for item in value]
    if isinstance(value, str):
        result = id_map.get(value, value).replace(f"projects/{source_id}/", f"projects/{target_id}/")
        # Apply the same identifier replacement used for copied path components.  IDs are
        # UUID-like opaque values, so an exact substring replacement is safe and also
        # handles identifiers at the start/end of filenames.
        return _replace_ids(result, id_map)
    return value


def _rewrite_json_artifacts(target_dir: Path, source_id: str, target_id: str, id_map: dict[str, str]) -> None:
    for path in target_dir.rglob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rewritten = _rewrite_value(value, source_id, target_id, id_map)
        path.write_text(json.dumps(rewritten, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def _refresh_and_verify_artifact_metadata(value: Any, data_root: Path) -> None:
    if isinstance(value, dict):
        uri = value.get("relative_uri")
        if isinstance(uri, str):
            path = (data_root / Path(uri)).resolve()
            root = data_root.resolve()
            if root not in path.parents or not path.is_file():
                raise AppError(
                    "CLONE_ARTIFACT_MISSING",
                    "A cloned resource refers to an artifact that was not copied.",
                    status_code=500,
                    details={"relative_uri": uri},
                )
            content = path.read_bytes()
            value["sha256"] = hashlib.sha256(content).hexdigest()
            value["size_bytes"] = len(content)
        for item in value.values():
            _refresh_and_verify_artifact_metadata(item, data_root)
    elif isinstance(value, list):
        for item in value:
            _refresh_and_verify_artifact_metadata(item, data_root)
