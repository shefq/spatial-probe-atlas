from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


_CONTEXT_FIELDS = (
    "trace_id",
    "correlation_id",
    "job_id",
    "session_id",
    "project_id",
    "duration_ms",
    "compute_mode",
    "error_code",
)
_SECRET_KEY = re.compile(r"(?:token|secret|authorization|cookie|spa_session)", re.IGNORECASE)
_CONTENT_KEY = re.compile(r"(?:^|_)(?:content|body|image|rgb|depth|raw_frame|upload)(?:$|_)", re.IGNORECASE)
_INLINE_SECRET = re.compile(
    r"(?i)(token|authorization|cookie|spa_session)(\s*[:=]\s*)([^\s,;&]+)"
)
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:\\|\\\\)[^\r\n\t\"']+")
_POSIX_USER_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:home|Users)/[^\r\n\t\"']+")


def _utc_timestamp(created: float | None = None) -> str:
    value = datetime.fromtimestamp(created, tz=UTC) if created is not None else datetime.now(UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact_text(value: str, *, data_root: Path | None = None, support_bundle: bool = False) -> str:
    """Redact secrets everywhere and local user paths when material leaves local logs."""

    redacted = _INLINE_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", value)
    if data_root is not None:
        variants = {str(data_root), data_root.as_posix()}
        for candidate in sorted((item for item in variants if item), key=len, reverse=True):
            redacted = redacted.replace(candidate, "<data-root>")
    if support_bundle:
        redacted = _WINDOWS_PATH.sub("<local-path>", redacted)
        redacted = _POSIX_USER_PATH.sub("<local-path>", redacted)
    return redacted


def redact_document(
    value: Any,
    *,
    data_root: Path | None = None,
    support_bundle: bool = False,
    key: str | None = None,
) -> Any:
    if key and _SECRET_KEY.search(key):
        return "<redacted>"
    if key and _CONTENT_KEY.search(key):
        return "<content-redacted>"
    if isinstance(value, str):
        return redact_text(value, data_root=data_root, support_bundle=support_bundle)
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_document(
                item,
                data_root=data_root,
                support_bundle=support_bundle,
                key=str(item_key),
            )
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            redact_document(item, data_root=data_root, support_bundle=support_bundle)
            for item in value
        ]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value), data_root=data_root, support_bundle=support_bundle)


class JsonLineFormatter(logging.Formatter):
    def __init__(self, data_root: Path) -> None:
        super().__init__()
        self.data_root = data_root

    def format(self, record: logging.LogRecord) -> str:
        fields = redact_document(
            getattr(record, "spa_fields", {}),
            data_root=self.data_root,
            support_bundle=False,
        )
        if not isinstance(fields, dict):
            fields = {}
        document: dict[str, Any] = {
            "timestamp": _utc_timestamp(record.created),
            "level": record.levelname,
            "component": record.name,
            "event": str(getattr(record, "spa_event", "log.message")),
            "message": redact_text(record.getMessage(), data_root=self.data_root),
        }
        for name in _CONTEXT_FIELDS:
            document[name] = fields.pop(name, None)
        document.update(fields)
        if record.exc_info:
            document["exception"] = redact_text(
                self.formatException(record.exc_info),
                data_root=self.data_root,
            )
        return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class ConciseConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = str(getattr(record, "spa_event", "log.message"))
        fields = getattr(record, "spa_fields", {})
        suffix = ""
        if isinstance(fields, Mapping):
            identity = fields.get("job_id") or fields.get("trace_id")
            if identity:
                suffix = f" [{str(identity)[:16]}]"
        return f"{record.levelname:<7} {event}{suffix}: {record.getMessage()}"


def _remove_owned_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_spa_owned", False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def _owned(handler: logging.Handler) -> logging.Handler:
    setattr(handler, "_spa_owned", True)
    return handler


def configure_logging(
    data_root: Path,
    level: str | int = "INFO",
    *,
    max_bytes: int = 10 * 1024 * 1024,
    file_count: int = 10,
) -> None:
    """Configure app/jobs JSONL rotation and one concise console stream."""

    log_dir = data_root.resolve() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    numeric_level = (
        level
        if isinstance(level, int)
        else getattr(logging, str(level).upper(), logging.INFO)
    )
    backups = max(0, min(int(file_count), 50) - 1)
    maximum = max(64 * 1024, min(int(max_bytes), 100 * 1024 * 1024))

    app_logger = logging.getLogger("spatial_probe_atlas")
    jobs_logger = logging.getLogger("spatial_probe_atlas.jobs")
    _remove_owned_handlers(app_logger)
    _remove_owned_handlers(jobs_logger)

    console = _owned(logging.StreamHandler())
    console.setLevel(numeric_level)
    console.setFormatter(ConciseConsoleFormatter())

    app_file = _owned(
        RotatingFileHandler(
            log_dir / "app.jsonl",
            maxBytes=maximum,
            backupCount=backups,
            encoding="utf-8",
            delay=True,
        )
    )
    app_file.setLevel(numeric_level)
    app_file.setFormatter(JsonLineFormatter(data_root.resolve()))

    jobs_file = _owned(
        RotatingFileHandler(
            log_dir / "jobs.jsonl",
            maxBytes=maximum,
            backupCount=backups,
            encoding="utf-8",
            delay=True,
        )
    )
    jobs_file.setLevel(numeric_level)
    jobs_file.setFormatter(JsonLineFormatter(data_root.resolve()))

    app_logger.setLevel(numeric_level)
    app_logger.propagate = False
    app_logger.addHandler(app_file)
    app_logger.addHandler(console)

    jobs_logger.setLevel(numeric_level)
    jobs_logger.propagate = False
    jobs_logger.addHandler(jobs_file)
    # A separate console instance prevents closing the app handler from mutating jobs.
    jobs_console = _owned(logging.StreamHandler())
    jobs_console.setLevel(numeric_level)
    jobs_console.setFormatter(ConciseConsoleFormatter())
    jobs_logger.addHandler(jobs_console)


def log_event(
    logger: logging.Logger,
    event: str,
    message: str,
    *,
    level: int = logging.INFO,
    exc_info: Any = None,
    **fields: Any,
) -> None:
    logger.log(
        level,
        message,
        extra={"spa_event": event, "spa_fields": fields},
        exc_info=exc_info,
    )


def read_structured_log_tail(log_dir: Path, *, limit: int, data_root: Path) -> list[dict[str, Any]]:
    count = min(max(int(limit), 1), 1000)
    candidates = [
        path
        for pattern in ("app.jsonl*", "jobs.jsonl*", "performance.jsonl*")
        for path in log_dir.glob(pattern)
        if path.is_file()
    ]
    items: list[dict[str, Any]] = []
    for path in sorted(set(candidates), key=lambda item: item.stat().st_mtime, reverse=True)[:30]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
        except OSError:
            continue
        fallback = _utc_timestamp(path.stat().st_mtime)
        for line in lines:
            try:
                parsed = json.loads(line)
            except (TypeError, ValueError):
                parsed = {
                    "timestamp": fallback,
                    "level": "INFO",
                    "component": path.stem,
                    "event": "log.unstructured",
                    "message": line,
                }
            if not isinstance(parsed, dict):
                continue
            sanitized = redact_document(
                parsed,
                data_root=data_root,
                support_bundle=True,
            )
            sanitized["source"] = path.name
            items.append(sanitized)
    items.sort(key=lambda item: str(item.get("timestamp") or ""))
    return items[-count:]


__all__ = [
    "configure_logging",
    "log_event",
    "read_structured_log_tail",
    "redact_document",
    "redact_text",
]
