"""Safe, explicit migration of one prototype project into v1 artifacts.

The worker side of this module never opens the application database.  It copies the
user-selected source into same-volume staging, builds an immutable project directory and
returns a database plan.  ``publish_legacy_import`` is the short main-process commit step.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import struct
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
from sqlalchemy import select

from spatial_probe_atlas import __version__
from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.adapters.persistence.database import (
    JobRecord,
    ProjectRecord,
    ResourceRecord,
    project_dict,
    utcnow,
)
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.domain.validation import validate_probe_calibration, validate_project_name
from spatial_probe_atlas.pipelines.mapping.cpu import _write_ply
from spatial_probe_atlas.pipelines.mapping.tiles import build_octree_manifest, validate_octree_manifest
from spatial_probe_atlas.pipelines.probe import DEFAULT_BLOB_DETECTOR


Progress = Callable[[str, int, int, float, str], None]
Cancelled = Callable[[], bool]
STAGE_COUNT = 8
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
POINT_JSON_NAMES = {"pointcloud.json", "point_cloud.json", "points.json", "points3d.json"}
PROBE_NAMES = {"probe_calibration.json"}
BOARD_NAMES = {"aruco_board.json", "aruco_board_calibration.json", "calibrated_board.json"}
MAX_JSON_BYTES = 512 * 1024 * 1024
MAX_FILES = 250_000
MAX_VERTICES = 25_000_000
DEFAULT_T_MARKER_TIP = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, -0.100, 0.0, 0.0, 0.0, 1.0]


@dataclass(frozen=True)
class InventoryItem:
    relative_path: str
    size_bytes: int
    sha256: str
    category: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "category": self.category,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"sha256": _sha256(path), "size_bytes": path.stat().st_size}


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def validate_legacy_source(source: Path, data_root: Path) -> Path:
    """Validate only the explicitly supplied root; no discovery outside it occurs."""
    if not source.is_absolute():
        raise AppError("LEGACY_SOURCE_NOT_ABSOLUTE", "Choose an absolute legacy project directory.", status_code=422)
    if not source.exists() or not source.is_dir():
        raise AppError("LEGACY_SOURCE_NOT_FOUND", "The selected legacy project directory does not exist.", status_code=404)
    if source.is_symlink() or _is_reparse_point(source):
        raise AppError("LEGACY_SOURCE_LINK_NOT_ALLOWED", "A linked or junction source directory cannot be imported.", status_code=422)
    resolved_source = source.resolve()
    resolved_data = data_root.resolve()
    if resolved_source == resolved_data or resolved_data in resolved_source.parents or resolved_source in resolved_data.parents:
        raise AppError("LEGACY_SOURCE_OVERLAPS_DATA_ROOT", "The selected legacy directory may not overlap the Spatial Probe Atlas data root.", status_code=422)
    return resolved_source


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        if directory != root and (directory.is_symlink() or _is_reparse_point(directory)):
            raise AppError("LEGACY_SOURCE_LINK_NOT_ALLOWED", "The legacy directory contains a link or junction.", status_code=422, details={"relative_path": directory.relative_to(root).as_posix()})
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise AppError("LEGACY_SOURCE_UNREADABLE", "A legacy source directory could not be read.", status_code=422, details={"relative_path": directory.relative_to(root).as_posix()}) from exc
        for entry in entries:
            if entry.is_symlink() or _is_reparse_point(entry):
                raise AppError("LEGACY_SOURCE_LINK_NOT_ALLOWED", "The legacy directory contains a link or junction.", status_code=422, details={"relative_path": entry.relative_to(root).as_posix()})
            if entry.is_dir():
                pending.append(entry)
            elif entry.is_file():
                files.append(entry)
                if len(files) > MAX_FILES:
                    raise AppError("LEGACY_SOURCE_TOO_MANY_FILES", f"A legacy import is limited to {MAX_FILES:,} files.", status_code=422)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().casefold())


def _copy_source_to_staging(source: Path, destination: Path, cancelled: Cancelled) -> list[InventoryItem]:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=False)
    inventory: list[InventoryItem] = []
    for source_file in _walk_files(source):
        if cancelled():
            raise InterruptedError("legacy import cancelled")
        relative = source_file.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        source_stat = source_file.stat()
        source_hash = hashlib.sha256()
        target_hash = hashlib.sha256()
        with source_file.open("rb") as reader, target.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                if cancelled():
                    raise InterruptedError("legacy import cancelled")
                source_hash.update(chunk)
                target_hash.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        digest = source_hash.hexdigest()
        if digest != target_hash.hexdigest():
            raise AppError("LEGACY_STAGING_CHECKSUM_MISMATCH", "A staged legacy file did not match its source checksum.", status_code=500, details={"relative_path": relative.as_posix()})
        source_stat_after = source_file.stat()
        if (source_stat.st_size, source_stat.st_mtime_ns) != (source_stat_after.st_size, source_stat_after.st_mtime_ns):
            raise AppError("LEGACY_SOURCE_CHANGED_DURING_COPY", "A legacy source file changed while it was being staged; retry after external writes stop.", status_code=409, details={"relative_path": relative.as_posix()}, retryable=True)
        inventory.append(InventoryItem(relative.as_posix(), target.stat().st_size, digest, "unknown"))
    return inventory


def _load_json(path: Path, *, maximum: int = MAX_JSON_BYTES) -> Any:
    size = path.stat().st_size
    if size > maximum:
        raise AppError("LEGACY_JSON_TOO_LARGE", "A legacy JSON artifact exceeds the safe import limit.", status_code=422, details={"file": path.name, "size_bytes": size, "limit_bytes": maximum})
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AppError("LEGACY_JSON_INVALID", "A recognized legacy JSON artifact could not be parsed.", status_code=422, details={"file": path.name}) from exc


def _rank_path(path: Path, root: Path, preferred_names: Iterable[str]) -> tuple[int, int, str]:
    relative = path.relative_to(root).as_posix().lower()
    name_order = {name: index for index, name in enumerate(preferred_names)}
    return (name_order.get(path.name.lower(), len(name_order)), 0 if any(part in relative for part in ("sfm_map", "outputs/sfm", "reconstruction")) else 1, relative)


def _find_colmap_directories(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory in {item.parent for item in root.rglob("*") if item.is_file() and item.name.lower() in {"cameras.bin", "cameras.txt"}}:
        names = {item.name.lower() for item in directory.iterdir() if item.is_file()}
        if ({"cameras.bin", "images.bin", "points3d.bin"} <= names) or ({"cameras.txt", "images.txt", "points3d.txt"} <= names):
            found.append(directory)
    return sorted(found, key=lambda path: (0 if "sfm" in path.as_posix().lower() else 1, path.relative_to(root).as_posix().lower()))


def _points_from_colmap(path: Path) -> tuple[np.ndarray, np.ndarray]:
    try:
        import pycolmap  # type: ignore
    except Exception as exc:
        raise AppError("LEGACY_COLMAP_READER_UNAVAILABLE", "pycolmap is required to migrate the detected COLMAP reconstruction.", status_code=503) from exc
    try:
        reconstruction = pycolmap.Reconstruction(str(path))
        values = list(reconstruction.points3D.values())
        points = np.asarray([value.xyz for value in values], dtype=np.float64)
        colours = np.asarray([value.color for value in values], dtype=np.uint8)
    except Exception as exc:
        raise AppError("LEGACY_COLMAP_INVALID", "The detected COLMAP reconstruction could not be opened.", status_code=422, details={"directory": path.name}) from exc
    return _validate_points(points, colours)


_PLY_TYPES: dict[str, tuple[str, int]] = {
    "char": ("i1", 1), "int8": ("i1", 1), "uchar": ("u1", 1), "uint8": ("u1", 1),
    "short": ("<i2", 2), "int16": ("<i2", 2), "ushort": ("<u2", 2), "uint16": ("<u2", 2),
    "int": ("<i4", 4), "int32": ("<i4", 4), "uint": ("<u4", 4), "uint32": ("<u4", 4),
    "float": ("<f4", 4), "float32": ("<f4", 4), "double": ("<f8", 8), "float64": ("<f8", 8),
}


def _points_from_ply(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with path.open("rb") as handle:
        first = handle.readline()
        if first.strip() != b"ply":
            raise AppError("LEGACY_PLY_INVALID", "The selected PLY file has no PLY header.", status_code=422)
        header_lines = ["ply"]
        header_size = len(first)
        while header_size < 1024 * 1024:
            line = handle.readline()
            if not line:
                break
            header_size += len(line)
            try:
                text = line.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise AppError("LEGACY_PLY_INVALID", "The PLY header is not ASCII.", status_code=422) from exc
            header_lines.append(text)
            if text == "end_header":
                break
        if not header_lines or header_lines[-1] != "end_header":
            raise AppError("LEGACY_PLY_INVALID", "The PLY header is incomplete.", status_code=422)
        format_line = next((line for line in header_lines if line.startswith("format ")), "")
        vertex_line = next((line for line in header_lines if line.startswith("element vertex ")), "")
        try:
            vertex_count = int(vertex_line.split()[2])
        except (IndexError, ValueError) as exc:
            raise AppError("LEGACY_PLY_INVALID", "The PLY vertex count is invalid.", status_code=422) from exc
        if not 1 <= vertex_count <= MAX_VERTICES:
            raise AppError("LEGACY_POINT_COUNT_INVALID", "The PLY vertex count is outside the v1 import limit.", status_code=422, details={"point_count": vertex_count})
        vertex_properties: list[tuple[str, str]] = []
        in_vertices = False
        for line in header_lines:
            if line.startswith("element "):
                in_vertices = line.startswith("element vertex ")
            elif in_vertices and line.startswith("property "):
                parts = line.split()
                if len(parts) != 3 or parts[1] == "list" or parts[1] not in _PLY_TYPES:
                    raise AppError("LEGACY_PLY_UNSUPPORTED", "Only scalar PLY vertex properties are supported.", status_code=422)
                vertex_properties.append((parts[2], parts[1]))
        names = {name for name, _ in vertex_properties}
        if not {"x", "y", "z"} <= names:
            raise AppError("LEGACY_PLY_INVALID", "PLY vertices must contain x, y, and z.", status_code=422)
        if "ascii" in format_line:
            rows: list[list[float]] = []
            for _ in range(vertex_count):
                line = handle.readline()
                if not line:
                    raise AppError("LEGACY_PLY_INVALID", "The PLY vertex body is truncated.", status_code=422)
                values = line.decode("ascii").split()
                if len(values) < len(vertex_properties):
                    raise AppError("LEGACY_PLY_INVALID", "A PLY vertex row is incomplete.", status_code=422)
                rows.append([float(value) for value in values[: len(vertex_properties)]])
            array = np.asarray(rows, dtype=np.float64)
            lookup = {name: array[:, index] for index, (name, _) in enumerate(vertex_properties)}
        elif "binary_little_endian" in format_line:
            dtype = np.dtype([(name, _PLY_TYPES[data_type][0]) for name, data_type in vertex_properties])
            array = np.fromfile(handle, dtype=dtype, count=vertex_count)
            if len(array) != vertex_count:
                raise AppError("LEGACY_PLY_INVALID", "The binary PLY vertex body is truncated.", status_code=422)
            lookup = {name: array[name] for name, _ in vertex_properties}
        else:
            raise AppError("LEGACY_PLY_UNSUPPORTED", "Only ASCII and binary little-endian PLY files are supported.", status_code=422)
    points = np.column_stack([lookup["x"], lookup["y"], lookup["z"]])
    colour_names = ("red", "green", "blue") if {"red", "green", "blue"} <= names else ("r", "g", "b") if {"r", "g", "b"} <= names else None
    colours = np.column_stack([lookup[name] for name in colour_names]) if colour_names else np.full((len(points), 3), 190, dtype=np.uint8)
    return _validate_points(points, colours)


def _points_from_json(path: Path) -> tuple[np.ndarray, np.ndarray]:
    value = _load_json(path)
    if not isinstance(value, dict):
        raise AppError("LEGACY_POINT_JSON_INVALID", "Legacy point-cloud JSON must be an object.", status_code=422)
    if isinstance(value.get("xyz"), list):
        raw = np.asarray(value["xyz"], dtype=np.float64)
        points = raw.reshape((-1, 3)) if raw.ndim == 1 and raw.size % 3 == 0 else raw
        raw_colours = value.get("rgb") or value.get("colors") or value.get("colours")
        colours = np.asarray(raw_colours, dtype=np.float64).reshape((-1, 3)) if isinstance(raw_colours, list) and raw_colours else np.full((len(points), 3), 190)
    elif isinstance(value.get("points"), list):
        entries = value["points"]
        if entries and isinstance(entries[0], dict):
            points = np.asarray([[item.get("x"), item.get("y"), item.get("z")] for item in entries], dtype=np.float64)
            colours = np.asarray([[item.get("r", 190), item.get("g", 190), item.get("b", 190)] for item in entries], dtype=np.float64)
        else:
            points = np.asarray(entries, dtype=np.float64)
            raw_colours = value.get("rgb") or value.get("colors") or value.get("colours")
            colours = np.asarray(raw_colours, dtype=np.float64) if isinstance(raw_colours, list) and raw_colours else np.full((len(points), 3), 190)
    else:
        raise AppError("LEGACY_POINT_JSON_INVALID", "Legacy point-cloud JSON contains neither xyz nor points.", status_code=422)
    return _validate_points(points, colours)


def _validate_points(points: np.ndarray, colours: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    points = np.asarray(points, dtype=np.float64)
    colours = np.asarray(colours)
    if points.ndim != 2 or points.shape[1] != 3 or not 1 <= len(points) <= MAX_VERTICES:
        raise AppError("LEGACY_POINT_COUNT_INVALID", "The legacy point cloud must contain finite XYZ vertices.", status_code=422)
    finite = np.isfinite(points).all(axis=1)
    if not finite.any():
        raise AppError("LEGACY_POINT_CLOUD_EMPTY", "The legacy point cloud contains no finite vertices.", status_code=422)
    points = points[finite]
    if colours.shape != (len(finite), 3):
        colours = np.full((len(finite), 3), 190, dtype=np.uint8)
    colours = np.nan_to_num(colours[finite], nan=190.0, posinf=255.0, neginf=0.0)
    if colours.dtype.kind == "f" and colours.size and float(np.nanmax(colours)) <= 1.0:
        colours = colours * 255.0
    return np.asarray(points, dtype="<f4"), np.clip(np.rint(colours), 0, 255).astype(np.uint8)


def _relative_artifact(project_id: str, relative: Path, path: Path) -> dict[str, Any]:
    return {
        "relative_uri": (Path("projects") / project_id / relative).as_posix(),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _build_octree_tiles(project_root: Path, project_id: str, map_id: str, points: np.ndarray, colours: np.ndarray) -> dict[str, Any]:
    return build_octree_manifest(
        project_root / "maps" / map_id,
        project_id=project_id,
        map_id=map_id,
        points=points,
        colours=colours,
        coordinate_frame="M0",
        units="arbitrary",
    )


def _extract_map(staged_source: Path, project_root: Path, project_id: str, used: set[str], warnings: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    points: np.ndarray | None = None
    colours: np.ndarray | None = None
    source_type: str | None = None
    source_path: Path | None = None
    colmap_directories = _find_colmap_directories(staged_source)
    for directory in colmap_directories:
        try:
            points, colours = _points_from_colmap(directory)
            source_type, source_path = "colmap", directory
            for item in directory.rglob("*"):
                if item.is_file():
                    used.add(item.relative_to(staged_source).as_posix())
            break
        except AppError as exc:
            warnings.append({"code": exc.code, "message": exc.message, "relative_path": directory.relative_to(staged_source).as_posix()})
    if points is None:
        ply_candidates = sorted(staged_source.rglob("*.ply"), key=lambda path: _rank_path(path, staged_source, ("point-cloud.ply", "pointcloud.ply", "points3d.ply")))
        for candidate in ply_candidates:
            try:
                points, colours = _points_from_ply(candidate)
                source_type, source_path = "ply", candidate
                used.add(candidate.relative_to(staged_source).as_posix())
                break
            except AppError as exc:
                warnings.append({"code": exc.code, "message": exc.message, "relative_path": candidate.relative_to(staged_source).as_posix()})
    if points is None:
        json_candidates = sorted((item for item in staged_source.rglob("*.json") if item.name.lower() in POINT_JSON_NAMES), key=lambda path: _rank_path(path, staged_source, ("pointcloud.json", "point_cloud.json", "points3d.json", "points.json")))
        for candidate in json_candidates:
            try:
                points, colours = _points_from_json(candidate)
                source_type, source_path = "point_json", candidate
                used.add(candidate.relative_to(staged_source).as_posix())
                break
            except AppError as exc:
                warnings.append({"code": exc.code, "message": exc.message, "relative_path": candidate.relative_to(staged_source).as_posix()})
    if points is None or colours is None or source_type is None or source_path is None:
        return None, None
    map_id = str(uuid.uuid4())
    map_dir = project_root / "maps" / map_id
    map_dir.mkdir(parents=True, exist_ok=True)
    ply_path = map_dir / "point-cloud.ply"
    ply_sha, ply_size = _write_ply(ply_path, points, colours, coordinate_frame="M0", units="arbitrary")
    if source_type == "colmap":
        preserved = map_dir / "legacy_colmap"
        shutil.copytree(source_path, preserved)
    manifest = _build_octree_tiles(project_root, project_id, map_id, points, colours)
    manifest["authoritative_ply"] = {
        "relative_uri": f"projects/{project_id}/maps/{map_id}/point-cloud.ply",
        "sha256": ply_sha,
        "size_bytes": ply_size,
        "point_count": int(len(points)),
    }
    manifest["provenance"] = {"method": "legacy_import", "source_type": source_type, "source_relative_path": source_path.relative_to(staged_source).as_posix()}
    validate_octree_manifest(map_dir, manifest, project_id=project_id, map_id=map_id)
    manifest_path = map_dir / "manifest.json"
    _atomic_json(manifest_path, manifest)
    manifest_artifact = _relative_artifact(project_id, Path("maps") / map_id / "manifest.json", manifest_path)
    map_payload = {
        "algorithm": "legacy_import_v1",
        "source_type": source_type,
        "source_relative_path": source_path.relative_to(staged_source).as_posix(),
        "point_count": int(len(points)),
        "registered_image_count": 0,
        "input_frame_count": 0,
        "registered_ratio": None,
        "mean_reprojection_error_px": None,
        "units": "arbitrary",
        "coordinate_frame": "M0",
        "ply": {"relative_uri": f"projects/{project_id}/maps/{map_id}/point-cloud.ply", "sha256": ply_sha, "size_bytes": ply_size},
        "manifest": manifest_artifact,
        "bounds": manifest["bounds"],
        "warnings": [{"code": "LEGACY_LOCALIZATION_INDEX_UNAVAILABLE", "message": "The imported map can be inspected, but must be rebuilt from frames before v1 live localization."}],
        "active": True,
    }
    resource = {"id": map_id, "kind": "scene_map", "parent_id": None, "name": "Imported legacy map", "state": "active", "payload": map_payload}
    return resource, {"source_type": source_type, "source_relative_path": map_payload["source_relative_path"], "point_count": len(points), "map_id": map_id}


def _matrix9(value: Any) -> list[float] | None:
    try:
        array = np.asarray(value, dtype=float)
        if array.shape == (3, 3):
            array = array.reshape(-1)
        if array.shape != (9,) or not np.isfinite(array).all() or array[0] <= 0 or array[4] <= 0:
            return None
        return array.astype(float).tolist()
    except (TypeError, ValueError):
        return None


def _load_intrinsics(staged_source: Path, image: Path, used: set[str]) -> list[float] | None:
    for directory in [image.parent, *image.parents]:
        if directory == staged_source.parent:
            break
        manifest_path = directory / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = _load_json(manifest_path, maximum=16 * 1024 * 1024)
                candidate = manifest.get(image.name) if isinstance(manifest, dict) else None
                matrix = _matrix9(candidate)
                if matrix:
                    used.add(manifest_path.relative_to(staged_source).as_posix())
                    return matrix
            except AppError:
                pass
        if directory == staged_source:
            break
    for candidate in sorted(staged_source.rglob("camera_calibration.json")):
        try:
            value = _load_json(candidate, maximum=4 * 1024 * 1024)
            matrix = _matrix9(value.get("intrinsic_matrix") or value.get("camera_matrix")) if isinstance(value, dict) else None
            if matrix:
                used.add(candidate.relative_to(staged_source).as_posix())
                return matrix
        except AppError:
            continue
    return None


def _decode_image(path: Path) -> tuple[np.ndarray, int, int]:
    try:
        import cv2  # type: ignore
    except Exception as exc:
        raise AppError("LEGACY_IMAGE_DECODER_UNAVAILABLE", "OpenCV is required to migrate legacy capture images.", status_code=503) from exc
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise AppError("LEGACY_IMAGE_INVALID", "A recognized legacy image could not be decoded.", status_code=422, details={"file": path.name})
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return np.ascontiguousarray(rgb, dtype=np.uint8), int(rgb.shape[1]), int(rgb.shape[0])


def _extract_capture(staged_source: Path, project_root: Path, project_id: str, used: set[str], warnings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    candidates = [
        item for item in staged_source.rglob("*")
        if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES and any(part.lower() in {"sfm", "images", "scene", "mapping"} for part in item.relative_to(staged_source).parts[:-1])
    ]
    candidates.sort(key=lambda item: item.relative_to(staged_source).as_posix().lower())
    if not candidates:
        return [], None
    capture_id = str(uuid.uuid4())
    resources: list[dict[str, Any]] = []
    accepted = 0
    byte_count = 0
    for index, image_path in enumerate(candidates):
        relative_source = image_path.relative_to(staged_source).as_posix()
        try:
            rgb, width, height = _decode_image(image_path)
        except AppError as exc:
            warnings.append({"code": exc.code, "message": exc.message, "relative_path": relative_source})
            continue
        used.add(relative_source)
        frame_id = str(uuid.uuid4())
        base = Path("captures") / capture_id / "frames" / frame_id
        rgb_path = project_root / base.with_suffix(".rgb8")
        rgb_path.parent.mkdir(parents=True, exist_ok=True)
        with rgb_path.open("wb") as handle:
            handle.write(rgb.tobytes())
            handle.flush()
            os.fsync(handle.fileno())
        source_copy = project_root / "captures" / capture_id / "source_images" / f"{index:06d}{image_path.suffix.lower()}"
        source_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image_path, source_copy)
        intrinsics = _load_intrinsics(staged_source, image_path, used)
        intrinsics_path = project_root / base.with_suffix(".intrinsics.json")
        intrinsics_document = {"schema_version": "1.0.0", "source": "legacy_import", "width": width, "height": height, "intrinsic_matrix": intrinsics, "source_relative_path": relative_source}
        _atomic_json(intrinsics_path, intrinsics_document)
        rgb_artifact = _relative_artifact(project_id, base.with_suffix(".rgb8"), rgb_path)
        intrinsic_artifact = _relative_artifact(project_id, base.with_suffix(".intrinsics.json"), intrinsics_path)
        source_artifact = _relative_artifact(project_id, Path("captures") / capture_id / "source_images" / source_copy.name, source_copy)
        byte_count += rgb_artifact["size_bytes"] + source_artifact["size_bytes"] + intrinsic_artifact["size_bytes"]
        state = "accepted" if intrinsics else "needs_intrinsics"
        accepted += int(bool(intrinsics))
        resources.append({
            "id": frame_id,
            "kind": "capture_frame",
            "parent_id": capture_id,
            "name": image_path.name,
            "state": state,
            "payload": {
                "sequence": index,
                "device_timestamp_ns": index,
                "width": width,
                "height": height,
                "intrinsic_matrix": intrinsics,
                "intrinsics_source": "legacy_manifest" if intrinsics else "missing",
                "rgb_artifact": rgb_artifact,
                "depth_artifact": None,
                "intrinsics_artifact": intrinsic_artifact,
                "source_image_artifact": source_artifact,
                "checksum": hashlib.sha256(rgb.tobytes()).hexdigest(),
                "quality": {"legacy_import": True},
                "included": bool(intrinsics),
                "source": "legacy_import",
                "source_relative_path": relative_source,
            },
        })
    if not resources:
        return [], None
    capture_state = "ready" if accepted == len(resources) else "requires_intrinsics"
    capture = {
        "id": capture_id,
        "kind": "capture_set",
        "parent_id": None,
        "name": "Imported legacy frames",
        "state": capture_state,
        "payload": {
            "source": "legacy_import",
            "frame_count": len(resources),
            "accepted_frame_count": accepted,
            "excluded_frame_count": len(resources) - accepted,
            "size_bytes": byte_count,
            "revision_source": "legacy_import_v1",
            "warnings": [] if accepted == len(resources) else [{"code": "LEGACY_FRAME_INTRINSICS_MISSING", "message": "Some imported frames need a compatible camera calibration before reuse."}],
        },
    }
    return [capture, *resources], {"capture_set_id": capture_id, "frame_count": len(resources), "accepted_frame_count": accepted, "state": capture_state}


def _created_at(value: dict[str, Any], source_path: Path) -> str:
    raw = value.get("created_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC).isoformat()
        except ValueError:
            pass
    raw_unix = value.get("created_unix_s")
    if isinstance(raw_unix, (int, float)) and math.isfinite(float(raw_unix)):
        try:
            return datetime.fromtimestamp(float(raw_unix), UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.fromtimestamp(source_path.stat().st_mtime, UTC).isoformat()


def _extract_probe(staged_source: Path, project_root: Path, project_id: str, project_name: str, used: set[str], confirm_defaults: bool, warnings: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    candidates = sorted((item for item in staged_source.rglob("*.json") if item.name.lower() in PROBE_NAMES), key=lambda path: _rank_path(path, staged_source, ("probe_calibration.json",)))
    config_candidates = sorted(staged_source.rglob("config.json"), key=lambda path: path.relative_to(staged_source).as_posix().lower())
    config: dict[str, Any] = {}
    for path in config_candidates:
        try:
            value = _load_json(path, maximum=4 * 1024 * 1024)
            if isinstance(value, dict):
                config.update(value)
                used.add(path.relative_to(staged_source).as_posix())
        except AppError as exc:
            warnings.append({"code": exc.code, "message": exc.message, "relative_path": path.relative_to(staged_source).as_posix()})
    for source_path in candidates:
        try:
            value = _load_json(source_path, maximum=16 * 1024 * 1024)
            if not isinstance(value, dict):
                raise AppError("LEGACY_PROBE_INVALID", "Legacy probe calibration must be a JSON object.", status_code=422)
            probe_value = value.get("probe") if isinstance(value.get("probe"), dict) else {}
            marker_points = (
                probe_value.get("marker_points_m")
                or probe_value.get("points_probe_m")
                or probe_value.get("dot_positions")
                or probe_value.get("points_3d")
                or value.get("marker_points_m")
                or value.get("dot_positions")
                or value.get("points_3d")
            )
            points = np.asarray(marker_points, dtype=float)
            if points.shape != (5, 3) or not np.isfinite(points).all():
                raise AppError("LEGACY_PROBE_GEOMETRY_INVALID", "Legacy probe geometry must contain five finite 3D marker points.", status_code=422)
            defaulted: list[str] = []
            tip = probe_value.get("t_marker_tip") or value.get("t_marker_tip")
            if tip is not None:
                transform = np.asarray(tip, dtype=float)
                if transform.shape == (4, 4):
                    transform = transform.reshape(-1)
                t_marker_tip = transform.astype(float).tolist() if transform.shape == (16,) and np.isfinite(transform).all() else None
            else:
                t_marker_tip = None
            if t_marker_tip is None and isinstance(probe_value.get("tip_point_probe_m"), list) and len(probe_value["tip_point_probe_m"]) == 3:
                tip_point = np.asarray(probe_value["tip_point_probe_m"], dtype=float)
                t_marker_tip = [1.0, 0.0, 0.0, float(tip_point[0]), 0.0, 1.0, 0.0, float(tip_point[1]), 0.0, 0.0, 1.0, float(tip_point[2]), 0.0, 0.0, 0.0, 1.0]
            if t_marker_tip is None:
                t_marker_tip = list(DEFAULT_T_MARKER_TIP)
                defaulted.append("probe.t_marker_tip")
            blob_sources: list[dict[str, Any]] = []
            for candidate in (value.get("blob_detector"), probe_value.get("blob_detector"), config.get("blob_detector"), config.get("blob_params")):
                if isinstance(candidate, dict):
                    blob_sources.append(candidate)
            merged: dict[str, Any] = {}
            for blob_source in blob_sources:
                merged.update({key: blob_source[key] for key in DEFAULT_BLOB_DETECTOR if key in blob_source})
            blob = dict(DEFAULT_BLOB_DETECTOR)
            blob.update(merged)
            defaulted.extend(f"blob_detector.{key}" for key in DEFAULT_BLOB_DETECTOR if key not in merged)
            quality_source = value.get("quality") if isinstance(value.get("quality"), dict) else {}
            input_count = int(quality_source.get("input_frame_count", value.get("input_frame_count", 0)) or 0)
            accepted_count = int(quality_source.get("accepted_frame_count", value.get("accepted_frame_count", 0)) or 0)
            rms = quality_source.get("rms_reprojection_error_px", value.get("final_error"))
            if not isinstance(rms, (int, float)) or not math.isfinite(float(rms)) or float(rms) < 0:
                rms = 0.0
                defaulted.append("quality.rms_reprojection_error_px")
            if "input_frame_count" not in quality_source and "input_frame_count" not in value:
                defaulted.append("quality.input_frame_count")
            if "accepted_frame_count" not in quality_source and "accepted_frame_count" not in value:
                defaulted.append("quality.accepted_frame_count")
            defaulted = sorted(set(defaulted))
            if defaulted and not confirm_defaults:
                raise AppError(
                    "LEGACY_PROBE_DEFAULT_CONFIRMATION_REQUIRED",
                    "The legacy probe calibration is incomplete; explicitly confirm the listed v1 defaults before importing.",
                    status_code=409,
                    details={"defaulted_fields": defaulted, "source_relative_path": source_path.relative_to(staged_source).as_posix()},
                    suggested_action="Review the defaulted fields, then retry with confirmation enabled.",
                )
            calibration_id = str(uuid.uuid4())
            portable = {
                "schema_version": "1.0.0",
                "calibration_id": calibration_id,
                "name": str(value.get("name") or "Imported legacy five-marker probe")[:120],
                "created_at": _created_at(value, source_path),
                "units": "m",
                "probe": {"model": "polaris_5_blob", "marker_frame": "M", "tip_frame": "P", "marker_points_m": points.astype(float).tolist(), "t_marker_tip": t_marker_tip},
                "blob_detector": blob,
                "quality": {
                    "input_frame_count": max(0, input_count),
                    "accepted_frame_count": max(0, min(accepted_count, input_count)),
                    "rms_reprojection_error_px": float(rms),
                    "notes": "Imported from the prototype; fields listed in migration_provenance.defaulted_fields were explicitly confirmed." if defaulted else "Imported from a complete prototype calibration.",
                },
                "provenance": {"application_version": __version__, "method": "imported", "source_calibration_id": None, "source_project_name": project_name},
            }
            errors = validate_probe_calibration(portable)
            if errors:
                raise AppError("LEGACY_PROBE_NORMALIZATION_FAILED", "The normalized legacy probe calibration did not pass v1 validation.", status_code=422, details={"errors": errors})
            target = project_root / "calibrations" / "probe" / f"{calibration_id}.json"
            _atomic_json(target, portable)
            artifact = _relative_artifact(project_id, Path("calibrations") / "probe" / target.name, target)
            used.add(source_path.relative_to(staged_source).as_posix())
            payload = {
                **portable,
                "artifact": artifact,
                "checksum": hashlib.sha256(json.dumps(portable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest(),
                "migration_provenance": {"source_relative_path": source_path.relative_to(staged_source).as_posix(), "defaulted_fields": defaulted, "defaults_confirmed": bool(defaulted and confirm_defaults)},
                "active": True,
            }
            return {"id": calibration_id, "kind": "probe_calibration", "parent_id": None, "name": portable["name"], "state": "active", "payload": payload}, {"probe_calibration_id": calibration_id, "source_relative_path": payload["migration_provenance"]["source_relative_path"], "defaulted_fields": defaulted, "defaults_confirmed": payload["migration_provenance"]["defaults_confirmed"]}
        except AppError as exc:
            if exc.code == "LEGACY_PROBE_DEFAULT_CONFIRMATION_REQUIRED":
                raise
            warnings.append({"code": exc.code, "message": exc.message, "relative_path": source_path.relative_to(staged_source).as_posix(), "details": exc.details})
    return None, None


def _similarity_from_config(config: dict[str, Any]) -> dict[str, Any] | None:
    direct = config.get("S_W_M0") or config.get("similarity_s_w_m0")
    if isinstance(direct, dict):
        try:
            scale = float(direct["scale"])
            rotation = np.asarray(direct["rotation"], dtype=float).reshape(3, 3)
            translation = np.asarray(direct["translation"], dtype=float).reshape(3)
            if scale > 0 and np.isfinite(rotation).all() and np.isfinite(translation).all() and np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-4) and np.linalg.det(rotation) > 0:
                return {"scale": scale, "rotation": rotation.reshape(-1).tolist(), "translation": translation.tolist()}
        except (KeyError, TypeError, ValueError):
            pass
    raw_scale = config.get("SFM_SCALE")
    if isinstance(raw_scale, (int, float)) and math.isfinite(float(raw_scale)) and float(raw_scale) > 0:
        return {"scale": float(raw_scale), "rotation": np.eye(3).reshape(-1).tolist(), "translation": [0.0, 0.0, 0.0]}
    return None


def _extract_registration(staged_source: Path, map_resource: dict[str, Any] | None, probe_resource: dict[str, Any] | None, used: set[str], warnings: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if map_resource is None:
        return None, None
    config: dict[str, Any] = {}
    for path in sorted(staged_source.rglob("config.json")):
        try:
            value = _load_json(path, maximum=4 * 1024 * 1024)
            if isinstance(value, dict):
                config.update(value)
                used.add(path.relative_to(staged_source).as_posix())
        except AppError:
            pass
    similarity = _similarity_from_config(config)
    board_document: dict[str, Any] | None = None
    board_path: Path | None = None
    for candidate in sorted((item for item in staged_source.rglob("*.json") if item.name.lower() in BOARD_NAMES), key=lambda path: _rank_path(path, staged_source, ("aruco_board.json", "aruco_board_calibration.json", "calibrated_board.json"))):
        try:
            value = _load_json(candidate, maximum=16 * 1024 * 1024)
            if isinstance(value, dict):
                aruco = value.get("aruco") if isinstance(value.get("aruco"), dict) else {}
                board = value.get("board") if isinstance(value.get("board"), dict) else value
                markers = board.get("markers") if isinstance(board.get("markers"), dict) else None
                if markers:
                    board_document = {
                        "dictionary": aruco.get("dictionary", "DICT_4X4_50"),
                        "marker_ids": [int(item) for item in aruco.get("marker_ids", markers.keys())],
                        "anchor_id": aruco.get("anchor_id"),
                        "marker_size_m": aruco.get("marker_size_m"),
                        "marker_corners_b_m": markers,
                        "source_units": value.get("units", "unknown"),
                    }
                    board_path = candidate
                    used.add(candidate.relative_to(staged_source).as_posix())
                    break
        except (AppError, TypeError, ValueError) as exc:
            warnings.append({"code": getattr(exc, "code", "LEGACY_BOARD_INVALID"), "message": str(exc), "relative_path": candidate.relative_to(staged_source).as_posix()})
    if similarity is None and board_document is None:
        return None, None
    registration_id = str(uuid.uuid4())
    payload = {
        "map_id": map_resource["id"],
        "map_revision": 1,
        "probe_calibration_id": probe_resource["id"] if probe_resource else None,
        "board_definition": board_document,
        "observation_count": 0,
        "similarity_s_w_m0": similarity,
        "scale": similarity["scale"] if similarity else None,
        "validation_status": "not_run",
        "migration_status": "requires_repeat_validation",
        "source_board_relative_path": board_path.relative_to(staged_source).as_posix() if board_path else None,
        "warnings": [{"code": "LEGACY_REGISTRATION_REVALIDATION_REQUIRED", "message": "Legacy scale/board metadata was normalized but is not active until v1 observations are solved and validated."}],
    }
    return {"id": registration_id, "kind": "registration", "parent_id": None, "name": "Imported legacy registration metadata", "state": "draft", "payload": payload}, {"registration_id": registration_id, "has_similarity": similarity is not None, "has_board_definition": board_document is not None, "validation_status": "not_run"}


def _copy_unknown_files(staged_source: Path, project_root: Path, inventory: list[InventoryItem], used: set[str], cancelled: Cancelled) -> list[dict[str, Any]]:
    unknown: list[dict[str, Any]] = []
    inventory_by_path = {item.relative_path: item for item in inventory}
    for relative, item in inventory_by_path.items():
        if relative in used:
            continue
        if cancelled():
            raise InterruptedError("legacy import cancelled")
        source = staged_source / Path(relative)
        target = project_root / "legacy_unmapped" / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        checksum = _sha256(target)
        if checksum != item.sha256:
            raise AppError("LEGACY_UNMAPPED_CHECKSUM_MISMATCH", "A preserved unknown file failed checksum verification.", status_code=500, details={"relative_path": relative})
        unknown.append({"source_relative_path": relative, "preserved_relative_uri": (Path("legacy_unmapped") / Path(relative)).as_posix(), "size_bytes": item.size_bytes, "sha256": checksum})
    return unknown


def _project_name(staged_source: Path, requested: str | None) -> str:
    if requested:
        return validate_project_name(requested)
    for path in sorted(staged_source.rglob("config.json")):
        try:
            value = _load_json(path, maximum=4 * 1024 * 1024)
            if isinstance(value, dict):
                candidate = value.get("project_name") or value.get("name")
                if isinstance(candidate, str):
                    return validate_project_name(candidate)
        except AppError:
            pass
    try:
        return validate_project_name(staged_source.name.replace("_", " "))
    except AppError:
        return "Imported legacy project"


def build_legacy_import(
    store: ArtifactStore,
    *,
    job_id: str,
    project_id: str,
    source_directory: str,
    requested_project_name: str | None,
    confirm_defaulted_probe_settings: bool,
    progress: Progress,
    cancelled: Cancelled,
) -> dict[str, Any]:
    """Build and validate a legacy project entirely under ``.staging``."""
    try:
        uuid.UUID(project_id)
    except ValueError as exc:
        raise AppError("LEGACY_PROJECT_ID_INVALID", "The frozen import target ID is invalid.", status_code=422) from exc
    source = validate_legacy_source(Path(source_directory), store.root)
    job_root = store.staging / job_id / "legacy-import"
    source_stage = job_root / "source"
    project_stage = job_root / "project"
    if job_root.exists():
        shutil.rmtree(job_root)
    job_root.mkdir(parents=True, exist_ok=False)
    progress("copy", 1, STAGE_COUNT, 0.0, "Copying the explicitly selected source into same-volume staging")
    inventory = _copy_source_to_staging(source, source_stage, cancelled)
    progress("inventory", 2, STAGE_COUNT, 1.0, f"Checksummed {len(inventory):,} staged files")
    project_name = _project_name(source_stage, requested_project_name)
    project_stage.mkdir(parents=True, exist_ok=False)
    used: set[str] = set()
    warnings: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    capture_resources, capture_summary = _extract_capture(source_stage, project_stage, project_id, used, warnings)
    resources.extend(capture_resources)
    progress("frames", 3, STAGE_COUNT, 1.0, f"Normalized {len(capture_resources) - (1 if capture_resources else 0)} legacy capture frames")
    map_resource, map_summary = _extract_map(source_stage, project_stage, project_id, used, warnings)
    if map_resource:
        if capture_summary:
            map_resource["parent_id"] = capture_summary["capture_set_id"]
            map_resource["payload"].update({"capture_set_id": capture_summary["capture_set_id"], "capture_set_revision": 1, "input_frame_count": capture_summary["frame_count"]})
        resources.append(map_resource)
    progress("map", 4, STAGE_COUNT, 1.0, "Converted the preferred legacy reconstruction" if map_resource else "No convertible legacy reconstruction was found")
    probe_resource, probe_summary = _extract_probe(source_stage, project_stage, project_id, project_name, used, confirm_defaulted_probe_settings, warnings)
    if probe_resource:
        resources.append(probe_resource)
    progress("probe", 5, STAGE_COUNT, 1.0, "Normalized the legacy probe calibration" if probe_resource else "No valid legacy probe calibration was found")
    registration_resource, registration_summary = _extract_registration(source_stage, map_resource, probe_resource, used, warnings)
    if registration_resource:
        resources.append(registration_resource)
    progress("registration", 6, STAGE_COUNT, 1.0, "Normalized scale and board metadata" if registration_resource else "No semantically sufficient registration metadata was found")
    unknown = _copy_unknown_files(source_stage, project_stage, inventory, used, cancelled)
    categorized_inventory: list[dict[str, Any]] = []
    unknown_paths = {item["source_relative_path"] for item in unknown}
    for item in inventory:
        category = "legacy_unmapped" if item.relative_path in unknown_paths else "recognized"
        categorized_inventory.append({**item.as_dict(), "category": category})
    report = {
        "schema_version": "1.0.0",
        "application_version": __version__,
        "migration_method": "prototype_explicit_import",
        "created_at": datetime.now(UTC).isoformat(),
        "source": {"display_name": source.name, "path_sha256": hashlib.sha256(str(source).encode("utf-8")).hexdigest(), "source_path_redacted": True},
        "target_project": {"project_id": project_id, "name": project_name},
        "inventory": categorized_inventory,
        "recognized_file_count": len(inventory) - len(unknown),
        "unknown_file_count": len(unknown),
        "unknown_files": unknown,
        "capture": capture_summary,
        "map": map_summary,
        "probe_calibration": probe_summary,
        "registration": registration_summary,
        "sessions_created": 0,
        "paint_paths_created": 0,
        "warnings": warnings,
        "validation": {"source_copied_to_same_volume_staging": True, "source_writes": 0, "all_inventory_files_checksummed": True, "unknown_files_preserved": True, "no_sessions_invented": True},
    }
    report_path = project_stage / "migration-report.json"
    _atomic_json(report_path, report)
    report_artifact = _relative_artifact(project_id, Path("migration-report.json"), report_path)
    plan = {
        "schema_version": 1,
        "project": {
            "id": project_id,
            "name": project_name,
            "state": "active",
            "active_map_id": map_resource["id"] if map_resource else None,
            "active_probe_calibration_id": probe_resource["id"] if probe_resource else None,
            "active_registration_id": None,
            "active_camera_calibration_id": None,
        },
        "resources": resources,
        "report": report_artifact,
        "report_summary": {"recognized_file_count": report["recognized_file_count"], "unknown_file_count": report["unknown_file_count"], "warnings": len(warnings)},
    }
    plan_path = job_root / "database-plan.json"
    _atomic_json(plan_path, plan)
    progress("validate", 7, STAGE_COUNT, 1.0, "Validated checksums, portable calibration, map manifest, and database plan")
    _validate_staged_project(project_stage, plan)
    progress("ready_to_publish", 8, STAGE_COUNT, 1.0, "The imported project is validated and ready for atomic publication")
    return {
        "project_id": project_id,
        "project_name": project_name,
        "staged_project_relative": project_stage.relative_to(store.root).as_posix(),
        "database_plan_relative": plan_path.relative_to(store.root).as_posix(),
        "report": report_artifact,
        "report_summary": plan["report_summary"],
    }


def _validate_staged_project(project_stage: Path, plan: dict[str, Any]) -> None:
    if not (project_stage / "migration-report.json").is_file():
        raise AppError("LEGACY_STAGED_PROJECT_INVALID", "The staged migration report is missing.", status_code=500)
    for resource in plan["resources"]:
        payload = resource.get("payload", {})
        for key in ("artifact", "ply", "manifest", "rgb_artifact", "intrinsics_artifact", "source_image_artifact"):
            artifact = payload.get(key)
            if not isinstance(artifact, dict) or not artifact.get("relative_uri"):
                continue
            relative = Path(artifact["relative_uri"])
            try:
                local_relative = relative.relative_to(Path("projects") / plan["project"]["id"])
            except ValueError as exc:
                raise AppError("LEGACY_PLAN_PATH_INVALID", "A staged artifact path is outside the target project.", status_code=500) from exc
            path = project_stage / local_relative
            if not path.is_file() or _sha256(path) != artifact["sha256"]:
                raise AppError("LEGACY_STAGED_CHECKSUM_INVALID", "A staged artifact failed final checksum validation.", status_code=500, details={"relative_uri": artifact["relative_uri"]})
        if resource["kind"] == "scene_map" and isinstance(payload.get("manifest"), dict):
            manifest_relative = Path(payload["manifest"]["relative_uri"]).relative_to(Path("projects") / plan["project"]["id"])
            manifest = _load_json(project_stage / manifest_relative, maximum=64 * 1024 * 1024)
            validate_octree_manifest(
                project_stage / manifest_relative.parent,
                manifest,
                project_id=plan["project"]["id"],
                map_id=resource["id"],
            )
    kinds = {resource["kind"] for resource in plan["resources"]}
    if "session" in kinds or "painted_point" in kinds or "painted_path" in kinds:
        raise AppError("LEGACY_PLAN_INVENTED_SESSION", "Prototype import must not invent sessions or paint records.", status_code=500)


def publish_legacy_import(database: Any, store: ArtifactStore, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Atomically rename the validated directory, then commit its database plan.

    If the database transaction fails, only this exact newly published project directory is
    removed.  Re-entry is idempotent after a crash between the database commit and job update.
    """
    project_id = str(result["project_id"])
    expected_job_root = (store.staging / job_id).resolve()
    try:
        expected_job_root.relative_to(store.staging.resolve())
    except ValueError as exc:
        raise AppError("LEGACY_JOB_PATH_INVALID", "The legacy import job path is outside staging.", status_code=500) from exc
    plan_path = expected_job_root / "legacy-import" / "database-plan.json"
    staged_project = expected_job_root / "legacy-import" / "project"
    expected_plan_relative = plan_path.relative_to(store.root).as_posix()
    expected_project_relative = staged_project.relative_to(store.root).as_posix()
    if result.get("database_plan_relative") != expected_plan_relative or result.get("staged_project_relative") != expected_project_relative:
        raise AppError("LEGACY_WORKER_RESULT_PATH_INVALID", "The legacy worker returned an unexpected publication path.", status_code=500)
    if not plan_path.is_file():
        raise AppError("LEGACY_DATABASE_PLAN_MISSING", "The validated legacy database plan is missing.", status_code=500)
    plan = _load_json(plan_path, maximum=64 * 1024 * 1024)
    if plan.get("project", {}).get("id") != project_id:
        raise AppError("LEGACY_DATABASE_PLAN_INVALID", "The legacy database plan target does not match its job.", status_code=500)
    final = store.projects / project_id
    with database.session() as session:
        existing = session.get(ProjectRecord, project_id)
        if existing is not None:
            report = final / "migration-report.json"
            if final.is_dir() and report.is_file() and _sha256(report) == result["report"]["sha256"]:
                job = session.get(JobRecord, job_id)
                if job is not None:
                    job.project_id = project_id
                    job.owner_id = project_id
                return {**result, "project": project_dict(existing), "publication_recovered": True}
            raise AppError("LEGACY_PROJECT_PUBLICATION_CONFLICT", "The target project ID already exists with different artifacts.", status_code=409)
    if final.exists():
        raise AppError("LEGACY_PROJECT_PUBLICATION_CONFLICT", "An untracked directory already uses the import target ID.", status_code=409)
    if not staged_project.is_dir():
        raise AppError("LEGACY_STAGED_PROJECT_MISSING", "The validated staged project directory is missing.", status_code=500)
    os.replace(staged_project, final)
    try:
        with database.session() as session:
            project_plan = plan["project"]
            project = ProjectRecord(
                id=project_id,
                name=validate_project_name(project_plan["name"]),
                state=project_plan.get("state", "active"),
                active_map_id=project_plan.get("active_map_id"),
                active_probe_calibration_id=project_plan.get("active_probe_calibration_id"),
                active_registration_id=project_plan.get("active_registration_id"),
                active_camera_calibration_id=project_plan.get("active_camera_calibration_id"),
            )
            session.add(project)
            for item in plan["resources"]:
                session.add(ResourceRecord(
                    id=item["id"],
                    project_id=project_id,
                    kind=item["kind"],
                    parent_id=item.get("parent_id"),
                    name=item.get("name"),
                    state=item["state"],
                    payload=item.get("payload", {}),
                ))
            job = session.get(JobRecord, job_id)
            if job is not None:
                job.project_id = project_id
                job.owner_id = project_id
                job.updated_at = utcnow()
            session.flush()
            response = project_dict(project)
    except Exception:
        if final.parent == store.projects and final.name == project_id and final.is_dir():
            shutil.rmtree(final)
        raise
    return {**result, "project": response, "published_at": datetime.now(UTC).isoformat()}


def migration_report_path(store: ArtifactStore, project_id: str, expected_sha256: str) -> Path:
    path = store.project_path(project_id, "migration-report.json")
    if not path.is_file() or _sha256(path) != expected_sha256:
        raise AppError("LEGACY_MIGRATION_REPORT_CORRUPT", "The migration report is missing or failed its checksum.", status_code=500)
    return path


__all__ = [
    "STAGE_COUNT",
    "build_legacy_import",
    "migration_report_path",
    "publish_legacy_import",
    "validate_legacy_source",
]
