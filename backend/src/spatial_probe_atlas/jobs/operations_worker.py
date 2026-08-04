from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sqlite3
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from spatial_probe_atlas import __version__
from spatial_probe_atlas.jobs.worker_ipc import atomic_json
from spatial_probe_atlas.observability import read_structured_log_tail, redact_document


class OperationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 500,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        suggested_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.retryable = retryable
        self.suggested_action = suggested_action


class WorkerCancelled(InterruptedError):
    pass


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_link(path: Path) -> bool:
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    reparse_flag = int(getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _sha256(path: Path, reporter: "Reporter | None" = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            if reporter is not None:
                reporter.check_cancelled()
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _atomic_copy(source: Path, destination: Path, reporter: "Reporter") -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.partial")
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as input_handle, partial.open("wb") as output_handle:
            while True:
                reporter.check_cancelled()
                block = input_handle.read(1024 * 1024)
                if not block:
                    break
                output_handle.write(block)
                digest.update(block)
                size += len(block)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.replace(partial, destination)
        try:
            shutil.copystat(source, destination, follow_symlinks=False)
        except OSError:
            pass
        return digest.hexdigest(), size
    finally:
        partial.unlink(missing_ok=True)


def _sqlite_integrity(database_path: Path) -> str:
    if not database_path.is_file():
        return "missing"
    connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        values = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        return "ok" if values == ["ok"] else "; ".join(values[:20])
    finally:
        connection.close()


@dataclass
class Reporter:
    progress_path: Path
    result_path: Path
    cancel_path: Path
    job_id: str
    current: dict[str, Any] | None = None
    last_write: float = 0.0

    def check_cancelled(self) -> None:
        if self.cancel_path.exists():
            raise WorkerCancelled("Worker cancellation was requested.")
        now = time.monotonic()
        if self.current is not None and now - self.last_write >= 4.0:
            heartbeat = {**self.current, "heartbeat_at": _timestamp()}
            atomic_json(self.progress_path, heartbeat)
            self.current, self.last_write = heartbeat, now

    def progress(
        self,
        stage: str,
        index: int,
        count: int,
        value: float,
        message: str,
        *,
        warnings: list[dict[str, Any]] | None = None,
        counters: dict[str, int] | None = None,
    ) -> None:
        self.check_cancelled()
        document = {
                "schema_version": 1,
                "job_id": self.job_id,
                "stage": stage,
                "stage_index": int(index),
                "stage_count": int(count),
                "progress": max(0.0, min(float(value), 1.0)),
                "message": message,
                "warnings": warnings or [],
                "counters": counters or {},
                "heartbeat_at": _timestamp(),
        }
        atomic_json(self.progress_path, document)
        self.current = document
        self.last_write = time.monotonic()


def _load_specification(specification_path: Path) -> tuple[dict[str, Any], Path, Reporter]:
    if not specification_path.is_file() or _is_link(specification_path):
        raise OperationError("WORKER_SPEC_INVALID", "The immutable worker specification is missing or unsafe.")
    try:
        document = json.loads(specification_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OperationError("WORKER_SPEC_INVALID", "The immutable worker specification is not valid JSON.") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise OperationError("WORKER_SPEC_INVALID", "The worker specification schema is unsupported.")
    job_type = document.get("type")
    if job_type not in {"support_bundle", "repair_reindex", "data_root_migration"}:
        raise OperationError("WORKER_SPEC_INVALID", "The operations worker received an unsupported job type.")
    try:
        job_id = str(uuid.UUID(str(document["job_id"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise OperationError("WORKER_SPEC_INVALID", "The worker job ID is invalid.") from exc
    data_root = _resolved(document.get("data_root", ""))
    if not data_root.is_dir() or _is_link(data_root):
        raise OperationError("DATA_ROOT_INVALID", "The source data root is unavailable or uses a filesystem link.")
    worker_root = (data_root / ".staging" / job_id).resolve(strict=False)
    if specification_path.resolve() != (worker_root / "worker-spec.json").resolve(strict=False):
        raise OperationError("WORKER_SPEC_PATH_INVALID", "The worker specification is outside its owned staging directory.")
    paths = {
        key: _resolved(document.get(key, ""))
        for key in ("progress_file", "result_file", "cancel_file")
    }
    for key, path in paths.items():
        if not _within(path, worker_root) or path.parent != worker_root:
            raise OperationError("WORKER_IPC_PATH_INVALID", f"The {key} path is outside the owned worker directory.")
    reporter = Reporter(paths["progress_file"], paths["result_file"], paths["cancel_file"], job_id)
    return document, data_root, reporter


def _manifest_documents(data_root: Path, reporter: Reporter) -> Iterator[tuple[Path, Any, str]]:
    projects = data_root / "projects"
    if not projects.is_dir():
        return
    for path in sorted(projects.rglob("manifest.json")):
        reporter.check_cancelled()
        resolved = path.resolve(strict=False)
        if not _within(resolved, projects.resolve()) or _is_link(path) or not path.is_file():
            continue
        if path.stat().st_size > 16 * 1024 * 1024:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            document = {"invalid": True}
        yield path, document, _sha256(path, reporter)


def _run_support_bundle(spec: dict[str, Any], data_root: Path, reporter: Reporter) -> dict[str, Any]:
    if bool(spec.get("include_raw_frames")):
        raise OperationError(
            "RAW_FRAMES_NOT_SUPPORTED",
            "V1 support bundles never include raw frames.",
            status_code=422,
        )
    support_root = (data_root / "support").resolve(strict=False)
    support_root.mkdir(parents=True, exist_ok=True)
    if not _within(support_root, data_root) or _is_link(support_root):
        raise OperationError("SUPPORT_PATH_INVALID", "The support output directory is unsafe.")
    target = support_root / f"support-{reporter.job_id}.zip"
    if target.exists() or _is_link(target):
        raise OperationError("SUPPORT_BUNDLE_EXISTS", "The support bundle output already exists.", status_code=409)
    staging = reporter.result_path.parent / "support-bundle.zip.partial"

    reporter.progress("collect_diagnostics", 1, 4, 0.2, "Collected redacted runtime and database diagnostics")
    database_path = (data_root / "app.db").resolve(strict=False)
    diagnostics = {
        "schema_version": "1.0.0",
        "application_version": str(spec.get("application_version") or __version__),
        "created_at": _timestamp(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "database_integrity": _sqlite_integrity(database_path),
        "raw_frames_included": False,
        "data_root": "<data-root>",
    }
    settings: dict[str, Any] = {"state": "not_present"}
    settings_path = data_root / "settings.json"
    if settings_path.is_file() and not _is_link(settings_path) and settings_path.stat().st_size <= 1024 * 1024:
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
            settings = redact_document(loaded, data_root=data_root, support_bundle=True)
        except (OSError, ValueError):
            settings = {"state": "invalid", "contents_included": False}

    reporter.progress("collect_logs", 2, 4, 0.5, "Collected bounded redacted structured logs")
    logs = read_structured_log_tail(data_root / "logs", limit=500, data_root=data_root)
    manifests = list(_manifest_documents(data_root, reporter))
    manifest_index: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("diagnostics.json", json.dumps(diagnostics, indent=2, sort_keys=True))
            archive.writestr("settings.redacted.json", json.dumps(settings, indent=2, sort_keys=True))
            archive.writestr("logs/recent.json", json.dumps(logs, indent=2, sort_keys=True))
            for path, document, checksum in manifests:
                reporter.check_cancelled()
                artifact_id = hashlib.sha256(path.relative_to(data_root).as_posix().encode("utf-8")).hexdigest()[:20]
                entry = f"manifests/{artifact_id}.json"
                sanitized = redact_document(document, data_root=data_root, support_bundle=True)
                archive.writestr(entry, json.dumps(sanitized, indent=2, sort_keys=True))
                manifest_index.append({"artifact_id": artifact_id, "sha256": checksum, "bundle_entry": entry})
            inventory = {
                "schema_version": 1,
                "manifest_count": len(manifest_index),
                "manifests": manifest_index,
                "raw_frames_included": False,
            }
            archive.writestr("inventory.json", json.dumps(inventory, indent=2, sort_keys=True))
        reporter.progress("redact_validate", 3, 4, 0.8, "Validated redaction and raw-frame exclusion")
        with staging.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    checksum = _sha256(target, reporter)
    reporter.progress("checksum", 4, 4, 1.0, "Published and verified the support bundle")
    return {
        "relative_uri": target.relative_to(data_root).as_posix(),
        "sha256": checksum,
        "size_bytes": target.stat().st_size,
        "raw_frames_included": False,
        "manifest_count": len(manifest_index),
    }


def _artifact_references(value: Any) -> Iterator[dict[str, str]]:
    if isinstance(value, dict):
        uri = value.get("relative_uri") or value.get("uri")
        checksum = value.get("sha256") or value.get("checksum_sha256")
        if isinstance(uri, str) and isinstance(checksum, str) and len(checksum) == 64:
            yield {"relative_uri": uri, "sha256": checksum.lower()}
        for item in value.values():
            yield from _artifact_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _artifact_references(item)


def _inventory_artifacts(data_root: Path, reporter: Reporter) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for manifest, document, manifest_checksum in _manifest_documents(data_root, reporter):
        manifest_id = hashlib.sha256(manifest.relative_to(data_root).as_posix().encode("utf-8")).hexdigest()[:20]
        records.append({"artifact_id": manifest_id, "kind": "manifest", "sha256": manifest_checksum, "status": "verified"})
        if not isinstance(document, (dict, list)) or (isinstance(document, dict) and document.get("invalid")):
            issues.append({"artifact_id": manifest_id, "code": "MANIFEST_INVALID"})
            continue
        for reference in _artifact_references(document):
            key = (reference["relative_uri"], reference["sha256"])
            if key in seen:
                continue
            seen.add(key)
            relative = Path(reference["relative_uri"])
            artifact_id = hashlib.sha256(reference["relative_uri"].encode("utf-8")).hexdigest()[:20]
            if relative.is_absolute() or ".." in relative.parts:
                issues.append({"artifact_id": artifact_id, "code": "ARTIFACT_PATH_UNSAFE"})
                continue
            path = (data_root / relative).resolve(strict=False)
            if not _within(path, data_root) or _is_link(path):
                issues.append({"artifact_id": artifact_id, "code": "ARTIFACT_PATH_UNSAFE"})
                continue
            if not path.is_file():
                issues.append({"artifact_id": artifact_id, "code": "ARTIFACT_MISSING"})
                continue
            observed = _sha256(path, reporter)
            status = "verified" if observed == reference["sha256"] else "checksum_mismatch"
            records.append({"artifact_id": artifact_id, "kind": "referenced_artifact", "sha256": observed, "expected_sha256": reference["sha256"], "size_bytes": path.stat().st_size, "status": status})
            if status != "verified":
                issues.append({"artifact_id": artifact_id, "code": "ARTIFACT_CHECKSUM_MISMATCH"})
    return records, issues


def _sqlite_backup_and_reindex(source: Path, destination: Path, reporter: Reporter) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    source_connection = sqlite3.connect(str(source))
    target_connection = sqlite3.connect(str(destination))
    try:
        def on_progress(_: int, __: int, ___: int) -> None:
            reporter.check_cancelled()

        source_connection.backup(target_connection, pages=256, progress=on_progress, sleep=0.01)
        target_connection.execute("REINDEX")
        target_connection.commit()
        values = [str(row[0]) for row in target_connection.execute("PRAGMA integrity_check")]
        return "ok" if values == ["ok"] else "; ".join(values[:20])
    finally:
        target_connection.close()
        source_connection.close()


def _run_repair(spec: dict[str, Any], data_root: Path, reporter: Reporter) -> dict[str, Any]:
    database_path = (data_root / "app.db").resolve(strict=False)
    if not database_path.is_file() or _is_link(database_path):
        raise OperationError("DATABASE_MISSING", "The database is missing or unsafe.")
    support_root = (data_root / "support").resolve(strict=False)
    support_root.mkdir(parents=True, exist_ok=True)
    if not _within(support_root, data_root) or _is_link(support_root):
        raise OperationError("REPAIR_OUTPUT_INVALID", "The repair output directory is unsafe.")

    original_integrity = _sqlite_integrity(database_path)
    reporter.progress("database_backup", 1, 4, 0.2, "Created a consistent candidate database backup")
    candidate_partial = reporter.result_path.parent / "repair-candidate.sqlite3.partial"
    candidate_target = support_root / f"repair-{reporter.job_id}.sqlite3"
    report_target = support_root / f"repair-{reporter.job_id}.json"
    if candidate_target.exists() or report_target.exists():
        raise OperationError("REPAIR_OUTPUT_EXISTS", "A repair output for this job already exists.", status_code=409)
    candidate_integrity = _sqlite_backup_and_reindex(database_path, candidate_partial, reporter)
    reporter.progress("candidate_reindex", 2, 4, 0.5, "Reindexed only the candidate database copy")

    artifacts, issues = _inventory_artifacts(data_root, reporter)
    reporter.progress(
        "artifact_inventory",
        3,
        4,
        0.8,
        f"Verified {len(artifacts)} manifest-backed artifacts",
        warnings=issues[:100],
        counters={"artifact_count": len(artifacts), "issue_count": len(issues)},
    )
    with candidate_partial.open("rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(candidate_partial, candidate_target)
    candidate_checksum = _sha256(candidate_target, reporter)
    report = {
        "schema_version": 1,
        "created_at": _timestamp(),
        "mode": "non_destructive_candidate",
        "original_database_integrity": original_integrity,
        "candidate_database_integrity": candidate_integrity,
        "original_modified": False,
        "replacement_requires_explicit_action": True,
        "candidate": {
            "relative_uri": candidate_target.relative_to(data_root).as_posix(),
            "sha256": candidate_checksum,
            "size_bytes": candidate_target.stat().st_size,
        },
        "artifact_inventory": artifacts,
        "issues": issues,
    }
    report_partial = reporter.result_path.parent / "repair-report.json.partial"
    atomic_json(report_partial, report)
    os.replace(report_partial, report_target)
    report_checksum = _sha256(report_target, reporter)
    reporter.progress("publish_report", 4, 4, 1.0, "Published the non-destructive repair report and candidate")
    return {
        "database_integrity": original_integrity,
        "candidate_database_integrity": candidate_integrity,
        "changed": False,
        "replacement_requires_explicit_action": True,
        "candidate_relative_uri": candidate_target.relative_to(data_root).as_posix(),
        "candidate_sha256": candidate_checksum,
        "report_relative_uri": report_target.relative_to(data_root).as_posix(),
        "report_sha256": report_checksum,
        "artifact_count": len(artifacts),
        "issue_count": len(issues),
    }


def _drive_is_fixed(path: Path) -> bool:
    if os.name != "nt":
        return True
    if str(path).startswith("\\\\"):
        return False
    try:
        import ctypes

        anchor = path.anchor or path.drive + "\\"
        return int(ctypes.windll.kernel32.GetDriveTypeW(str(anchor))) == 3
    except Exception:
        return False


def _migration_quiescent(database_path: Path, job_id: str) -> bool:
    connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    try:
        active_jobs = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE id <> ? AND state IN ('admitted','processing','cancelling')",
            (job_id,),
        ).fetchone()[0]
        active_sessions = connection.execute(
            "SELECT COUNT(*) FROM resources WHERE kind='session' AND state IN ('running','paused','degraded','stopping')"
        ).fetchone()[0]
        return int(active_jobs) == 0 and int(active_sessions) == 0
    finally:
        connection.close()


def _migration_sources(source: Path, reporter: Reporter) -> Iterator[tuple[Path, Path]]:
    excluded_directories = {".staging", "temp"}
    excluded_names = {"instance.json", "instance.lock", "app.db-wal", "app.db-shm"}
    for directory, directories, files in os.walk(source, topdown=True, followlinks=False):
        base = Path(directory)
        reporter.check_cancelled()
        safe_directories: list[str] = []
        for name in directories:
            child = base / name
            if name in excluded_directories:
                continue
            if _is_link(child):
                raise OperationError("DATA_ROOT_LINK_UNSAFE", "The data root contains a linked directory and cannot be migrated safely.", details={"entry_id": hashlib.sha256(child.relative_to(source).as_posix().encode()).hexdigest()[:20]})
            safe_directories.append(name)
        directories[:] = safe_directories
        for name in files:
            path = base / name
            if name in excluded_names or name.endswith(".partial") or name == "app.db":
                continue
            if _is_link(path):
                raise OperationError("DATA_ROOT_LINK_UNSAFE", "The data root contains a linked file and cannot be migrated safely.", details={"entry_id": hashlib.sha256(path.relative_to(source).as_posix().encode()).hexdigest()[:20]})
            relative = path.relative_to(source)
            if relative.is_absolute() or ".." in relative.parts:
                raise OperationError("DATA_ROOT_ENTRY_UNSAFE", "A data-root entry escaped the source root.")
            yield path, relative


def _prepare_destination_stage(stage: Path, destination_root: Path, job_id: str) -> None:
    marker = stage / ".spa-migration-staging.json"
    if stage.exists():
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise OperationError("DATA_ROOT_STAGING_CONFLICT", "An unrecognized migration staging directory already exists.", status_code=409)
        if value.get("job_id") != job_id or value.get("destination_name") != destination_root.name:
            raise OperationError("DATA_ROOT_STAGING_CONFLICT", "The migration staging directory belongs to another operation.", status_code=409)
        shutil.rmtree(stage)
    stage.mkdir(parents=False, exist_ok=False)
    atomic_json(marker, {"schema_version": 1, "job_id": job_id, "destination_name": destination_root.name})


def _run_data_root_migration(spec: dict[str, Any], source: Path, reporter: Reporter) -> dict[str, Any]:
    destination_root = _resolved(spec.get("destination_root", ""))
    if not destination_root.is_absolute() or not destination_root.name:
        raise OperationError("DATA_ROOT_DESTINATION_INVALID", "The migration destination is invalid.", status_code=422)
    if destination_root == source or _within(destination_root, source) or _within(source, destination_root):
        raise OperationError("DATA_ROOT_DESTINATION_OVERLAP", "Source and destination data roots may not overlap.", status_code=422)
    if not _drive_is_fixed(destination_root):
        raise OperationError("DATA_ROOT_DESTINATION_NOT_LOCAL", "V1 data-root migration supports fixed local drives only.", status_code=422)
    destination_parent = destination_root.parent
    destination_parent.mkdir(parents=True, exist_ok=True)
    if _is_link(destination_parent) or destination_root.exists() or _is_link(destination_root):
        raise OperationError("DATA_ROOT_DESTINATION_NOT_EMPTY", "The destination data-root directory must not already exist.", status_code=409)
    database_path = source / "app.db"
    if not database_path.is_file() or _is_link(database_path):
        raise OperationError("DATABASE_MISSING", "The source database is missing or unsafe.")
    if not _migration_quiescent(database_path, reporter.job_id):
        raise OperationError("DATA_ROOT_MIGRATION_BUSY", "Stop active sessions and other processing jobs before migration.", status_code=423, retryable=True)

    source_entries = list(_migration_sources(source, reporter))
    estimated_bytes = database_path.stat().st_size + sum(path.stat().st_size for path, _ in source_entries if path.exists())
    reserve = max(0, int(spec.get("disk_reserve_bytes", 0)))
    if shutil.disk_usage(destination_parent).free < estimated_bytes + reserve:
        raise OperationError("INSUFFICIENT_STORAGE", "The destination lacks migration size plus the configured reserve.", status_code=507, retryable=True)
    reporter.progress("inventory", 1, 4, 1.0, f"Inventoried {len(source_entries) + 1} durable files", counters={"file_count": len(source_entries) + 1, "estimated_bytes": estimated_bytes})

    stage = destination_parent / f".{destination_root.name}.{reporter.job_id}.partial"
    _prepare_destination_stage(stage, destination_root, reporter.job_id)
    inventory: list[dict[str, Any]] = []
    try:
        db_partial = stage / ".app.db.partial"
        candidate_integrity = _sqlite_backup_and_reindex(database_path, db_partial, reporter)
        # The migrated database is a consistent backup. REINDEX affects only this copy.
        os.replace(db_partial, stage / "app.db")
        db_checksum = _sha256(stage / "app.db", reporter)
        inventory.append({"relative_uri": "app.db", "sha256": db_checksum, "size_bytes": (stage / "app.db").stat().st_size})

        copied_bytes = inventory[0]["size_bytes"]
        last_update = 0.0
        for index, (path, relative) in enumerate(source_entries, start=1):
            reporter.check_cancelled()
            try:
                checksum, size = _atomic_copy(path, stage / relative, reporter)
            except FileNotFoundError:
                # Rotating logs may disappear while being copied; durable project data may not.
                if relative.parts and relative.parts[0] == "logs":
                    continue
                raise OperationError("DATA_ROOT_SOURCE_CHANGED", "A durable source file disappeared during migration.", retryable=True)
            inventory.append({"relative_uri": relative.as_posix(), "sha256": checksum, "size_bytes": size})
            copied_bytes += size
            now = time.monotonic()
            if now - last_update >= 0.2 or index == len(source_entries):
                reporter.progress(
                    "copy",
                    2,
                    4,
                    min(copied_bytes / max(estimated_bytes, 1), 1.0),
                    f"Copied {index + 1} / {len(source_entries) + 1} files",
                    counters={"files_copied": index + 1, "bytes_copied": copied_bytes},
                )
                last_update = now

        reporter.progress("verify", 3, 4, 0.0, "Verifying every copied file checksum")
        for index, item in enumerate(inventory, start=1):
            path = (stage / item["relative_uri"]).resolve(strict=False)
            if not _within(path, stage.resolve()) or not path.is_file() or _is_link(path):
                raise OperationError("DATA_ROOT_MIGRATION_VERIFY_FAILED", "A migrated file is missing or unsafe.")
            if _sha256(path, reporter) != item["sha256"] or path.stat().st_size != item["size_bytes"]:
                raise OperationError("DATA_ROOT_MIGRATION_VERIFY_FAILED", "A migrated file failed checksum verification.")
            if index % 20 == 0 or index == len(inventory):
                reporter.progress("verify", 3, 4, index / max(len(inventory), 1), f"Verified {index} / {len(inventory)} files")

        completion = {
            "schema_version": 1,
            "completed_at": _timestamp(),
            "source": "<previous-data-root>",
            "database_integrity": candidate_integrity,
            "file_count": len(inventory),
            "total_bytes": sum(int(item["size_bytes"]) for item in inventory),
            "inventory": inventory,
            "restart_required": True,
        }
        atomic_json(stage / "migration-complete.json", completion)
        (stage / ".spa-migration-staging.json").unlink(missing_ok=True)
        os.replace(stage, destination_root)
    except Exception:
        # Deliberately retain only the clearly marked, unpublished staging directory for diagnostics/resume.
        raise

    marker = destination_root / "migration-complete.json"
    reporter.progress("publish", 4, 4, 1.0, "Published the verified data-root copy; restart is required")
    return {
        "destination": str(destination_root),
        "restart_required": True,
        "source_unchanged": True,
        "file_count": len(inventory),
        "total_bytes": sum(int(item["size_bytes"]) for item in inventory),
        "migration_manifest_sha256": _sha256(marker, reporter),
    }


def run(specification_path: Path) -> dict[str, Any]:
    spec, data_root, reporter = _load_specification(specification_path)
    reporter.progress("starting", 0, int(spec.get("stage_count") or 4), 0.0, "Validated immutable worker specification")
    if spec["type"] == "support_bundle":
        return _run_support_bundle(spec, data_root, reporter)
    if spec["type"] == "repair_reindex":
        return _run_repair(spec, data_root, reporter)
    return _run_data_root_migration(spec, data_root, reporter)


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    specification_path = Path(sys.argv[1]).resolve(strict=False)
    result_path: Path | None = None
    try:
        # Resolve the result path before execution so even validation failures are structured when safe.
        raw = json.loads(specification_path.read_text(encoding="utf-8"))
        candidate = _resolved(raw.get("result_file", "")) if isinstance(raw, dict) else None
        if candidate is not None and candidate.parent == specification_path.parent:
            result_path = candidate
        result = run(specification_path)
        if result_path is None:
            return 2
        atomic_json(result_path, {"schema_version": 1, "result": result, "finished_at": _timestamp()})
        return 0
    except WorkerCancelled as exc:
        if result_path is not None:
            atomic_json(result_path, {"schema_version": 1, "error": {"code": "JOB_CANCELLED", "message": str(exc), "status_code": 409, "retryable": True}})
        return 2
    except OperationError as exc:
        if result_path is not None:
            atomic_json(
                result_path,
                {
                    "schema_version": 1,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "status_code": exc.status_code,
                        "details": exc.details,
                        "retryable": exc.retryable,
                        "suggested_action": exc.suggested_action,
                    },
                },
            )
        return 1
    except Exception as exc:
        if result_path is not None:
            atomic_json(result_path, {"schema_version": 1, "error": {"code": "OPERATIONS_WORKER_FAILED", "message": str(exc), "status_code": 500, "retryable": True}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run"]
