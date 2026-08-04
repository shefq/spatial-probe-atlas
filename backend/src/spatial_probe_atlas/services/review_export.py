from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import math
import struct
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import numpy as np
from sqlalchemy import String, and_, cast, func, or_, select

from spatial_probe_atlas import __version__
from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.adapters.persistence.database import Database, ResourceRecord, resource_dict
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.domain.validation import safe_relative_path


ACTIVE_REVIEW_STATES = frozenset({"running", "paused", "degraded", "stopping"})
EXPORT_FORMATS = frozenset({"json", "csv", "session_manifest", "screenshot", "point_overlay"})
REVIEW_TYPES = frozenset({"all", "point", "path"})
REVIEW_QUALITIES = frozenset({"all", "good", "warning", "low", "flagged_low_quality"})
_FILTER_FIELDS = frozenset({"type", "quality", "from", "to", "include_deleted"})
_RESOURCE_METADATA = frozenset(
    {
        "id",
        "project_id",
        "kind",
        "parent_id",
        "name",
        "state",
        "revision",
        "deleted",
        "created_at",
        "updated_at",
        "map_id",
        "probe_calibration_id",
        "registration_id",
    }
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_utc(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _sha_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise AppError(
            "REVIEW_FILTER_INVALID",
            f"{field} must be an RFC 3339 UTC timestamp.",
            status_code=422,
            details={"field": field},
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError(
            "REVIEW_FILTER_INVALID",
            f"{field} must be an RFC 3339 UTC timestamp.",
            status_code=422,
            details={"field": field},
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise AppError(
            "REVIEW_FILTER_INVALID",
            f"{field} must include the UTC offset Z or +00:00.",
            status_code=422,
            details={"field": field},
        )
    return parsed.astimezone(UTC)


def freeze_review_filters(
    value: Mapping[str, Any] | None,
    *,
    include_deleted: bool | None = None,
    forced_type: str | None = None,
) -> dict[str, Any]:
    """Validate and canonicalize a review/export filter into an immutable JSON snapshot."""

    raw = dict(value or {})
    unknown = sorted(set(raw) - _FILTER_FIELDS)
    if unknown:
        raise AppError(
            "REVIEW_FILTER_INVALID",
            "The review filter contains unsupported fields.",
            status_code=422,
            details={"unsupported_fields": unknown},
        )
    record_type = forced_type or str(raw.get("type", "all")).lower()
    quality = str(raw.get("quality", "all")).lower()
    if record_type not in REVIEW_TYPES:
        raise AppError("REVIEW_FILTER_INVALID", "type must be all, point, or path.", status_code=422, details={"field": "type"})
    if quality not in REVIEW_QUALITIES:
        raise AppError(
            "REVIEW_FILTER_INVALID",
            "quality must be all, good, warning, low, or flagged_low_quality.",
            status_code=422,
            details={"field": "quality"},
        )
    start = _parse_utc(raw.get("from"), "from")
    end = _parse_utc(raw.get("to"), "to")
    if start and end and start > end:
        raise AppError(
            "REVIEW_TIME_RANGE_INVALID",
            "The review start time must not be after the end time.",
            status_code=422,
            details={"from": _format_utc(start), "to": _format_utc(end)},
        )
    if "include_deleted" in raw and not isinstance(raw["include_deleted"], bool):
        raise AppError(
            "REVIEW_FILTER_INVALID",
            "include_deleted must be a boolean.",
            status_code=422,
            details={"field": "include_deleted"},
        )
    if include_deleted is not None and "include_deleted" in raw and bool(raw["include_deleted"]) != bool(include_deleted):
        raise AppError(
            "REVIEW_FILTER_INVALID",
            "include_deleted must agree between the export request and its filter snapshot.",
            status_code=422,
            details={"field": "include_deleted"},
        )
    deleted = bool(raw.get("include_deleted", False)) if include_deleted is None else bool(include_deleted)
    return {
        "type": record_type,
        "quality": quality,
        "from": _format_utc(start) if start else None,
        "to": _format_utc(end) if end else None,
        "include_deleted": deleted,
    }


def ensure_session_review_mutable(session: Mapping[str, Any]) -> None:
    if str(session.get("state")) in ACTIVE_REVIEW_STATES:
        raise AppError(
            "SESSION_REVIEW_READ_ONLY",
            "An active session is read-only in Review. Stop the session before annotating, deleting, or restoring records.",
            status_code=409,
            details={"session_id": session.get("session_id") or session.get("id"), "state": session.get("state")},
            suggested_action="Stop the live session, then retry the review change.",
        )


def paint_record_view(value: Mapping[str, Any]) -> dict[str, Any]:
    record_type = "point" if value.get("kind") == "painted_point" else "path"
    record_id = value.get("point_id") if record_type == "point" else value.get("path_id")
    result = {**value, "id": record_id or value.get("id"), "type": record_type, "session_id": value.get("parent_id") or value.get("session_id")}
    if record_type == "path":
        result.setdefault("positions_w_m", [sample["position_w_m"] for sample in result.get("samples", []) if "position_w_m" in sample])
        result.setdefault("sample_count", len(result["positions_w_m"]))
    return _jsonable(result)


def record_timestamp(value: Mapping[str, Any]) -> str:
    candidate = value.get("timestamp") or value.get("started_at") or value.get("created_at")
    parsed = _parse_utc(_format_utc(candidate) if isinstance(candidate, datetime) else candidate, "record timestamp")
    if parsed is None:
        raise AppError("PAINT_RECORD_INVALID", "A paint record has no timestamp.", status_code=500, details={"record_id": value.get("id")})
    return _format_utc(parsed)


def _time_expression() -> Any:
    # julianday accepts RFC 3339 Z/+00:00 forms and lets existing records with either
    # representation share one stable chronological order.
    timestamp = ResourceRecord.payload["timestamp"].as_string()
    started_at = ResourceRecord.payload["started_at"].as_string()
    fallback = cast(ResourceRecord.created_at, String)
    return func.julianday(func.coalesce(timestamp, started_at, fallback))


def _quality_values(value: str) -> tuple[str, ...]:
    if value == "low":
        return ("low", "flagged_low_quality")
    return (value,)


def _query_conditions(project_id: str, session_id: str, filters: Mapping[str, Any]) -> list[Any]:
    conditions: list[Any] = [ResourceRecord.project_id == project_id, ResourceRecord.parent_id == session_id]
    kinds = {
        "all": ("painted_point", "painted_path"),
        "point": ("painted_point",),
        "path": ("painted_path",),
    }[str(filters["type"])]
    conditions.append(ResourceRecord.kind.in_(kinds))
    if not filters["include_deleted"]:
        conditions.append(ResourceRecord.deleted.is_(False))
    if filters["quality"] != "all":
        conditions.append(ResourceRecord.payload["quality"].as_string().in_(_quality_values(str(filters["quality"]))))
    time_value = _time_expression()
    if filters.get("from"):
        conditions.append(time_value >= func.julianday(filters["from"]))
    if filters.get("to"):
        conditions.append(time_value <= func.julianday(filters["to"]))
    return conditions


def _filter_fingerprint(filters: Mapping[str, Any]) -> str:
    return _sha_json(filters)


def _encode_cursor(timestamp: str, record_id: str, filters: Mapping[str, Any]) -> str:
    raw = _canonical_json({"v": 1, "t": timestamp, "i": record_id, "f": _filter_fingerprint(filters)})
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str, filters: Mapping[str, Any]) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        timestamp, record_id = str(value["t"]), str(value["i"])
        if value.get("v") != 1 or value.get("f") != _filter_fingerprint(filters):
            raise ValueError("cursor belongs to another filter")
        _parse_utc(timestamp, "cursor")
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise AppError(
            "REVIEW_CURSOR_INVALID",
            "The review cursor is invalid or belongs to a different filter snapshot.",
            status_code=422,
        ) from exc
    return timestamp, record_id


def query_review_records(
    database: Database,
    project_id: str,
    session_id: str,
    filters: Mapping[str, Any],
    *,
    cursor: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        raise AppError("REVIEW_LIMIT_INVALID", "limit must be between 1 and 1000.", status_code=422)
    frozen = freeze_review_filters(filters)
    conditions = _query_conditions(project_id, session_id, frozen)
    time_value = _time_expression()
    page_conditions = list(conditions)
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor, frozen)
        cursor_julian = func.julianday(cursor_time)
        page_conditions.append(or_(time_value > cursor_julian, and_(time_value == cursor_julian, ResourceRecord.id > cursor_id)))
    with database.session() as db:
        total = int(db.scalar(select(func.count()).select_from(ResourceRecord).where(*conditions)) or 0)
        rows = list(
            db.scalars(
                select(ResourceRecord)
                .where(*page_conditions)
                .order_by(time_value, ResourceRecord.id)
                .limit(limit + 1)
            )
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [paint_record_view(resource_dict(row)) for row in rows]
    next_cursor = _encode_cursor(record_timestamp(items[-1]), str(items[-1]["id"]), frozen) if has_more and items else None
    return {"items": items, "next_cursor": next_cursor, "total": total, "filters": frozen}


def list_review_records(database: Database, project_id: str, session_id: str, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
    frozen = freeze_review_filters(filters)
    conditions = _query_conditions(project_id, session_id, frozen)
    time_value = _time_expression()
    with database.session() as db:
        rows = list(db.scalars(select(ResourceRecord).where(*conditions).order_by(time_value, ResourceRecord.id)))
    return [paint_record_view(resource_dict(row)) for row in rows]


def _resource_content(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in _RESOURCE_METADATA}


def _immutable_revision_refs(catalog: Any, project_id: str, session: Mapping[str, Any]) -> dict[str, Any]:
    definitions = (
        ("map", "scene_map", "map_id", "map_revision"),
        ("probe_calibration", "probe_calibration", "probe_calibration_id", "probe_calibration_revision"),
        ("registration", "registration", "registration_id", "registration_revision"),
    )
    refs: dict[str, Any] = {}
    for label, kind, id_field, revision_field in definitions:
        resource_id = session.get(id_field)
        if not resource_id:
            refs[label] = None
            continue
        resource = catalog.get_resource(project_id, kind, str(resource_id))
        content = _resource_content(resource)
        refs[label] = {
            "resource_id": str(resource_id),
            "revision": int(session.get(revision_field) or resource["revision"]),
            "content_sha256": _sha_json(content),
        }
        manifest = resource.get("manifest")
        artifact = resource.get("artifact")
        if isinstance(manifest, Mapping) and manifest.get("sha256"):
            refs[label]["artifact_sha256"] = manifest["sha256"]
        elif isinstance(artifact, Mapping) and artifact.get("sha256"):
            refs[label]["artifact_sha256"] = artifact["sha256"]
    return refs


def _session_descriptor(catalog: Any, project_id: str, session: Mapping[str, Any]) -> dict[str, Any]:
    refs = _immutable_revision_refs(catalog, project_id, session)
    return {
        "session_id": session.get("session_id") or session.get("id"),
        "name": session.get("name"),
        "state": session.get("state"),
        "revision": int(session.get("revision", 1)),
        "started_at": _jsonable(session.get("started_at")),
        "ended_at": _jsonable(session.get("ended_at")),
        "frame_count": int(session.get("frame_count", 0)),
        "immutable_revision_refs": refs,
    }


def _record_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    shared = {
        "id": value["id"],
        "type": value["type"],
        "revision": int(value.get("revision", 1)),
        "quality": value.get("quality"),
        "note": value.get("note", ""),
        "deleted": bool(value.get("deleted", False)),
        "coordinate_frame": value.get("coordinate_frame", "W"),
        "units": value.get("units", "m"),
    }
    if value["type"] == "point":
        shared.update(
            {
                "timestamp": record_timestamp(value),
                "frame_id": value.get("frame_id"),
                "position_w_m": value.get("position_w_m"),
                "orientation_w_xyzw": value.get("orientation_w_xyzw"),
                "metrics": value.get("metrics", {}),
                "override_reason": value.get("override_reason"),
            }
        )
    else:
        samples = []
        for sample in value.get("samples", []):
            samples.append(
                {
                    "timestamp": record_timestamp(sample),
                    "position_w_m": sample.get("position_w_m"),
                    "quality": sample.get("quality", value.get("quality")),
                }
            )
        shared.update(
            {
                "started_at": record_timestamp(value),
                "ended_at": value.get("ended_at"),
                "sampling_policy": value.get("sampling_policy", {}),
                "sample_count": len(samples),
                "length_m": float(value.get("length_m", 0.0)),
                "samples": samples,
            }
        )
    return _jsonable(shared)


def _export_material(
    catalog: Any,
    database: Database,
    project_id: str,
    session_id: str,
    filters: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, str], dict[str, int]]:
    session = catalog.get_resource(project_id, "session", session_id)
    records = list_review_records(database, project_id, session_id, filters)
    points = [_record_payload(item) for item in records if item["type"] == "point"]
    paths = [_record_payload(item) for item in records if item["type"] == "path"]
    descriptor = _session_descriptor(catalog, project_id, session)
    checksums = {
        "filters_sha256": _sha_json(filters),
        "session_reference_sha256": _sha_json(descriptor),
        "points_sha256": _sha_json(points),
        "paths_sha256": _sha_json(paths),
    }
    counts = {
        "points": len(points),
        "paths": len(paths),
        "path_samples": sum(int(item["sample_count"]) for item in paths),
        "records": len(points) + len(paths),
    }
    return descriptor, points, paths, checksums, counts


def _base_document(
    export_type: str,
    frozen_at: str,
    session: Mapping[str, Any],
    filters: Mapping[str, Any],
    checksums: Mapping[str, str],
    counts: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "export_type": export_type,
        "application_version": __version__,
        "generated_at": frozen_at,
        "session": session,
        "coordinate_frame": "W",
        "units": "m",
        "coordinate_system": {
            "frame": "W",
            "units": "m",
            "handedness": "right_handed",
            "transform_convention": "T_A_B maps coordinates from frame B into frame A",
        },
        "filters": dict(filters),
        "record_counts": dict(counts),
        "checksums": dict(checksums),
    }


def _csv_bytes(
    session: Mapping[str, Any],
    filters: Mapping[str, Any],
    points: Iterable[Mapping[str, Any]],
    paths: Iterable[Mapping[str, Any]],
    checksums: Mapping[str, str],
) -> bytes:
    fields = [
        "row_kind", "schema_version", "application_version", "session_id", "session_revision",
        "map_id", "map_revision", "probe_calibration_id", "probe_calibration_revision",
        "registration_id", "registration_revision", "coordinate_frame", "units", "filters_sha256",
        "record_type", "record_id", "record_revision", "sample_index", "timestamp",
        "x_m", "y_m", "z_m", "quality", "note", "deleted", "record_sha256",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    refs = session["immutable_revision_refs"]
    metadata = {
        "row_kind": "metadata", "schema_version": "1.0.0", "application_version": __version__,
        "session_id": session["session_id"], "session_revision": session["revision"],
        "map_id": (refs.get("map") or {}).get("resource_id"), "map_revision": (refs.get("map") or {}).get("revision"),
        "probe_calibration_id": (refs.get("probe_calibration") or {}).get("resource_id"),
        "probe_calibration_revision": (refs.get("probe_calibration") or {}).get("revision"),
        "registration_id": (refs.get("registration") or {}).get("resource_id"),
        "registration_revision": (refs.get("registration") or {}).get("revision"),
        "coordinate_frame": "W", "units": "m", "filters_sha256": checksums["filters_sha256"],
    }
    writer.writerow(metadata)

    def write_data(record: Mapping[str, Any], sample_index: int, timestamp: Any, position: Any, quality: Any) -> None:
        xyz = position if isinstance(position, list) and len(position) == 3 else (None, None, None)
        writer.writerow(
            {
                **metadata,
                "row_kind": "record",
                "record_type": record["type"],
                "record_id": record["id"],
                "record_revision": record["revision"],
                "sample_index": sample_index,
                "timestamp": timestamp,
                "x_m": xyz[0], "y_m": xyz[1], "z_m": xyz[2],
                "quality": quality,
                "note": record.get("note", ""),
                "deleted": str(bool(record.get("deleted"))).lower(),
                "record_sha256": _sha_json(record),
            }
        )

    for point in points:
        write_data(point, 0, point.get("timestamp"), point.get("position_w_m"), point.get("quality"))
    for path in paths:
        for index, sample in enumerate(path.get("samples", [])):
            write_data(path, index, sample.get("timestamp"), sample.get("position_w_m"), sample.get("quality", path.get("quality")))
    return output.getvalue().encode("utf-8")


def _project_positions(points: Iterable[Mapping[str, Any]], paths: Iterable[Mapping[str, Any]]) -> tuple[list[tuple[Mapping[str, Any], np.ndarray]], list[tuple[Mapping[str, Any], list[np.ndarray]]]]:
    projected_points: list[tuple[Mapping[str, Any], np.ndarray]] = []
    projected_paths: list[tuple[Mapping[str, Any], list[np.ndarray]]] = []
    for point in points:
        value = np.asarray(point.get("position_w_m", []), dtype=float)
        if value.shape == (3,) and np.isfinite(value).all():
            projected_points.append((point, value))
    for path in paths:
        values = []
        for sample in path.get("samples", []):
            value = np.asarray(sample.get("position_w_m", []), dtype=float)
            if value.shape == (3,) and np.isfinite(value).all():
                values.append(value)
        projected_paths.append((path, values))
    return projected_points, projected_paths


def _quality_color(value: Any) -> np.ndarray:
    return {
        "good": np.asarray([39, 217, 171, 255], dtype=np.uint8),
        "warning": np.asarray([246, 190, 67, 255], dtype=np.uint8),
        "low": np.asarray([255, 102, 117, 255], dtype=np.uint8),
        "flagged_low_quality": np.asarray([255, 102, 117, 255], dtype=np.uint8),
    }.get(str(value), np.asarray([126, 156, 181, 255], dtype=np.uint8))


def _draw_line(image: np.ndarray, start: tuple[int, int], end: tuple[int, int], color: np.ndarray, radius: int = 1) -> None:
    x0, y0 = start
    x1, y1 = end
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    error = dx + dy
    while True:
        image[max(0, y0 - radius): min(image.shape[0], y0 + radius + 1), max(0, x0 - radius): min(image.shape[1], x0 + radius + 1)] = color
        if x0 == x1 and y0 == y1:
            break
        twice = 2 * error
        if twice >= dy:
            error += dy
            x0 += sx
        if twice <= dx:
            error += dx
            y0 += sy


def _png_chunk(kind: bytes, content: bytes) -> bytes:
    return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF)


def _png_rgba(image: np.ndarray, metadata: Mapping[str, str]) -> bytes:
    rows = b"".join(b"\x00" + row.tobytes() for row in np.ascontiguousarray(image, dtype=np.uint8))
    height, width = image.shape[:2]
    chunks = [_png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))]
    for key in sorted(metadata):
        chunks.append(_png_chunk(b"tEXt", key.encode("latin-1") + b"\x00" + metadata[key].encode("latin-1", "replace")))
    chunks.extend([_png_chunk(b"IDAT", zlib.compress(rows, 9)), _png_chunk(b"IEND", b"")])
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _orthographic_png(
    points: list[dict[str, Any]],
    paths: list[dict[str, Any]],
    *,
    transparent: bool,
    filters_sha256: str,
    source_sha256: str,
) -> tuple[bytes, dict[str, Any]]:
    width, height, margin = 1024, 768, 48
    image = np.zeros((height, width, 4), dtype=np.uint8)
    if not transparent:
        image[:] = [13, 20, 31, 255]
        for x in range(0, width, 64):
            image[:, x:x + 1] = [27, 41, 57, 255]
        for y in range(0, height, 64):
            image[y:y + 1, :] = [27, 41, 57, 255]
    point_values, path_values = _project_positions(points, paths)
    all_values = [value for _, value in point_values] + [value for _, values in path_values for value in values]
    bounds = None
    if all_values:
        xy = np.asarray([value[:2] for value in all_values])
        low, high = xy.min(axis=0), xy.max(axis=0)
        span = np.maximum(high - low, 1e-9)
        scale = min((width - 2 * margin) / span[0], (height - 2 * margin) / span[1])
        used = span * scale
        offset = np.asarray([(width - used[0]) / 2, (height - used[1]) / 2])

        def pixel(value: np.ndarray) -> tuple[int, int]:
            position = (value[:2] - low) * scale + offset
            return int(round(position[0])), height - 1 - int(round(position[1]))

        for path, values in path_values:
            color = _quality_color(path.get("quality"))
            if path.get("deleted"):
                color = np.asarray([126, 156, 181, 160], dtype=np.uint8)
            for left, right in zip(values, values[1:]):
                _draw_line(image, pixel(left), pixel(right), color, radius=1)
        for point, value in point_values:
            x, y = pixel(value)
            color = _quality_color(point.get("quality"))
            if point.get("deleted"):
                color = np.asarray([126, 156, 181, 160], dtype=np.uint8)
            image[max(0, y - 3): min(height, y + 4), max(0, x - 3): min(width, x + 4)] = color
        bounds = {"min_w_m": [float(low[0]), float(low[1])], "max_w_m": [float(high[0]), float(high[1])]}
    metadata = {
        "Software": f"Spatial Probe Atlas {__version__}",
        "Description": "Deterministic server-rendered orthographic W-XY paint export; not a browser-camera screenshot",
        "CoordinateFrame": "W",
        "Units": "m",
        "FilterSHA256": filters_sha256,
        "SourceSHA256": source_sha256,
    }
    return _png_rgba(image, metadata), {
        "renderer": "server_orthographic_v1",
        "view": "W_XY_top_down",
        "resolution_px": [width, height],
        "transparent": transparent,
        "includes_map": False,
        "deterministic": True,
        "bounds": bounds,
        "description": metadata["Description"],
    }


def run_session_export(
    catalog: Any,
    database: Database,
    artifacts: ArtifactStore,
    *,
    project_id: str,
    session_id: str,
    export_id: str,
    spec: Mapping[str, Any],
    progress: Callable[[str, int, int, float, str], None] | None = None,
) -> dict[str, Any]:
    report = progress or (lambda *_: None)
    format_name = str(spec.get("format", "json"))
    if format_name not in EXPORT_FORMATS:
        raise AppError(
            "EXPORT_FORMAT_UNSUPPORTED",
            "Supported v1 exports are JSON, CSV, session manifest, orthographic screenshot, and point overlay.",
            status_code=422,
        )
    frozen = freeze_review_filters(spec.get("filters"), include_deleted=spec.get("include_deleted"))
    frozen_at_value = spec.get("frozen_at") or datetime.now(UTC).isoformat()
    frozen_at = _format_utc(_parse_utc(str(frozen_at_value), "frozen_at") or datetime.now(UTC))
    session, points, paths, checksums, counts = _export_material(catalog, database, project_id, session_id, frozen)
    report("query", 1, 3, 1.0, f"Loaded {counts['points']} points and {counts['paths']} paths using the frozen filter")
    document = _base_document("session_records", frozen_at, session, frozen, checksums, counts)
    rendering = None
    media_type: str
    if format_name == "json":
        document.update({"points": points, "paths": paths})
        content = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        extension, media_type = "json", "application/json"
    elif format_name == "session_manifest":
        document["export_type"] = "session_manifest"
        content = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        extension, media_type = "json", "application/json"
    elif format_name == "csv":
        content = _csv_bytes(session, frozen, points, paths, checksums)
        extension, media_type = "csv", "text/csv; charset=utf-8"
    else:
        content, rendering = _orthographic_png(
            points,
            paths,
            transparent=format_name == "point_overlay",
            filters_sha256=checksums["filters_sha256"],
            source_sha256=_sha_json({"points": checksums["points_sha256"], "paths": checksums["paths_sha256"]}),
        )
        extension, media_type = "png", "image/png"
    report("write", 2, 3, 0.5, "Writing export through atomic staging")
    target = artifacts.project_path(project_id, Path("sessions") / session_id / "exports" / f"{export_id}.{extension}")
    artifact = artifacts.atomic_write_bytes(target, content)
    observed = artifacts.sha256(target)
    if observed != artifact["sha256"]:
        target.unlink(missing_ok=True)
        raise AppError("EXPORT_CHECKSUM_MISMATCH", "The export failed checksum verification and was not published.", status_code=500)
    report("checksum", 3, 3, 1.0, "Published and verified export checksum")
    return {
        "schema_version": "1.0.0",
        "format": format_name,
        "media_type": media_type,
        "relative_uri": artifact["relative_uri"],
        "sha256": artifact["sha256"],
        "checksum_sha256": artifact["sha256"],
        "size_bytes": artifact["size_bytes"],
        "filters": frozen,
        "filter_snapshot_sha256": checksums["filters_sha256"],
        "source_checksums": checksums,
        "record_counts": counts,
        "coordinate_frame": "W",
        "units": "m",
        "rendering": rendering,
        "completed_at": _format_utc(datetime.now(UTC)),
        "download_filename": f"spatial-probe-atlas-{session_id}-{format_name}.{extension}",
    }


def verify_export_artifact(artifacts: ArtifactStore, relative_uri: str, expected_sha256: str) -> Path:
    if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256.lower()):
        raise AppError("EXPORT_METADATA_INVALID", "The export checksum metadata is invalid.", status_code=409)
    path = safe_relative_path(artifacts.root, Path(relative_uri))
    if not path.is_file():
        raise AppError("EXPORT_ARTIFACT_MISSING", "The export artifact is missing.", status_code=409, retryable=False)
    observed = artifacts.sha256(path)
    if not hmac.compare_digest(observed.lower(), expected_sha256.lower()):
        raise AppError(
            "EXPORT_CHECKSUM_MISMATCH",
            "The export artifact no longer matches its recorded checksum and will not be downloaded.",
            status_code=409,
            details={"expected_sha256": expected_sha256.lower(), "observed_sha256": observed.lower()},
            suggested_action="Run repair/reindex or create the export again.",
        )
    return path
