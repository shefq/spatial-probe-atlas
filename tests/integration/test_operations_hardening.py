from __future__ import annotations

import json
import logging
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Any

from spatial_probe_atlas.observability import configure_logging, log_event, read_structured_log_tail


def _wait_for_job(client: Any, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["state"] in {"completed", "failed", "cancelled", "interrupted", "recoverable"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_structured_rotation_redaction_and_tail(tmp_path: Path) -> None:
    data_root = tmp_path / "logging-data"
    configure_logging(data_root, "INFO")
    app_logger = logging.getLogger("spatial_probe_atlas.app")
    file_handlers = [handler for handler in logging.getLogger("spatial_probe_atlas").handlers if hasattr(handler, "backupCount")]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == 10 * 1024 * 1024
    assert file_handlers[0].backupCount == 9

    secret = "never-print-this-token"
    log_event(
        app_logger,
        "test.redaction",
        f"token={secret} cookie={secret}",
        trace_id="trace-1",
        project_id="project-1",
        imported_file_content="private imported payload",
        local_path=str(data_root / "projects" / "private"),
    )
    document = json.loads((data_root / "logs" / "app.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert secret not in json.dumps(document)
    assert document["timestamp"].endswith("Z")
    assert document["level"] == "INFO"
    assert document["component"] == "spatial_probe_atlas.app"
    assert document["event"] == "test.redaction"
    assert document["trace_id"] == "trace-1"
    assert document["imported_file_content"] == "<content-redacted>"

    tail = read_structured_log_tail(data_root / "logs", limit=10, data_root=data_root)
    assert tail[-1]["event"] == "test.redaction"
    assert str(data_root) not in json.dumps(tail)


def test_request_and_job_logs_are_separate_jsonl(client: Any, app_settings: Any) -> None:
    trace_id = "request-trace-123"
    secret = "bootstrap-secret-never-log"
    response = client.get(f"/api/v1/health/live?token={secret}", headers={"X-Correlation-ID": trace_id})
    assert response.status_code == 200
    app_log = app_settings.data_root / "logs" / "app.jsonl"
    documents = [json.loads(line) for line in app_log.read_text(encoding="utf-8").splitlines()]
    request_event = next(item for item in reversed(documents) if item["event"] == "http.request.completed")
    assert request_event["trace_id"] == trace_id
    assert request_event["path"] == "/api/v1/health/live"
    assert secret not in json.dumps(documents)
    for field in ("timestamp", "level", "component", "event", "trace_id", "correlation_id", "job_id", "session_id", "project_id", "duration_ms", "compute_mode", "error_code"):
        assert field in request_event

    response = client.post("/api/v1/support-bundles", json={})
    assert response.status_code == 202, response.text
    job = _wait_for_job(client, response.json()["job_id"])
    assert job["state"] == "completed", job
    jobs_log = app_settings.data_root / "logs" / "jobs.jsonl"
    job_documents = [json.loads(line) for line in jobs_log.read_text(encoding="utf-8").splitlines()]
    assert any(item["event"] == "worker.started" and item["job_id"] == job["job_id"] for item in job_documents)
    assert any(item["event"] == "job.completed" and item["job_id"] == job["job_id"] for item in job_documents)

    tail = client.get("/api/v1/system/logs/tail?limit=50")
    assert tail.status_code == 200
    assert all(isinstance(item, dict) and "event" in item for item in tail.json()["items"])


def test_support_repair_and_data_root_migration_are_isolated_and_non_destructive(
    client: Any,
    app_settings: Any,
    tmp_path: Path,
) -> None:
    container = client.app.state.container
    project = container.catalog.create_project("Operations hardening")
    project_id = project["project_id"]
    manifest = container.artifacts.project_path(project_id, Path("maps") / "map-a" / "manifest.json")
    container.artifacts.atomic_write_json(
        manifest,
        {
            "schema_version": 1,
            "project_id": project_id,
            "relative_uri": f"projects/{project_id}/maps/map-a/manifest.json",
            "sha256": "0" * 64,
        },
    )
    raw_frame = container.artifacts.project_path(project_id, Path("capture") / "raw-frame.jpg")
    raw_frame.parent.mkdir(parents=True, exist_ok=True)
    raw_payload = b"RAW-FRAME-MUST-NOT-ENTER-SUPPORT-BUNDLE"
    raw_frame.write_bytes(raw_payload)

    support_response = client.post("/api/v1/support-bundles", json={})
    assert support_response.status_code == 202, support_response.text
    support_job = _wait_for_job(client, support_response.json()["job_id"])
    assert support_job["state"] == "completed", support_job
    support_path = app_settings.data_root / support_job["result"]["relative_uri"]
    assert support_path.is_file()
    with zipfile.ZipFile(support_path) as archive:
        assert {"diagnostics.json", "settings.redacted.json", "logs/recent.json", "inventory.json"}.issubset(archive.namelist())
        assert all(raw_payload not in archive.read(name) for name in archive.namelist())
        diagnostics = json.loads(archive.read("diagnostics.json"))
        assert diagnostics["raw_frames_included"] is False
    support_spec = app_settings.data_root / ".staging" / support_job["job_id"] / "worker-spec.json"
    immutable_support_spec = support_spec.read_bytes()
    assert json.loads(immutable_support_spec)["include_raw_frames"] is False
    assert support_spec.read_bytes() == immutable_support_spec

    repair_response = client.post("/api/v1/system/repair-reindex", json={"mode": "non_destructive_candidate"})
    assert repair_response.status_code == 202, repair_response.text
    repair_job = _wait_for_job(client, repair_response.json()["job_id"])
    assert repair_job["state"] == "completed", repair_job
    assert repair_job["result"]["changed"] is False
    assert repair_job["result"]["replacement_requires_explicit_action"] is True
    candidate = app_settings.data_root / repair_job["result"]["candidate_relative_uri"]
    report_path = app_settings.data_root / repair_job["result"]["report_relative_uri"]
    assert candidate.is_file() and report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["original_modified"] is False
    assert report["candidate_database_integrity"] == "ok"
    with sqlite3.connect(candidate) as database:
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    destination_parent = tmp_path / "new-local-root"
    migration_response = client.post(
        "/api/v1/system/data-root-migrations",
        json={"destination": str(destination_parent)},
    )
    assert migration_response.status_code == 202, migration_response.text
    migration_job = _wait_for_job(client, migration_response.json()["job_id"], timeout=60.0)
    assert migration_job["state"] == "completed", migration_job
    destination = Path(migration_job["result"]["destination"])
    assert destination == destination_parent / "SpatialProbeAtlas"
    assert (destination / "app.db").is_file()
    assert (destination / "migration-complete.json").is_file()
    assert (destination / raw_frame.relative_to(app_settings.data_root)).read_bytes() == raw_payload
    assert raw_frame.read_bytes() == raw_payload
    migration_manifest = json.loads((destination / "migration-complete.json").read_text(encoding="utf-8"))
    assert migration_manifest["restart_required"] is True
    assert migration_manifest["file_count"] == migration_job["result"]["file_count"]
    migration_spec = app_settings.data_root / ".staging" / migration_job["job_id"] / "worker-spec.json"
    frozen = json.loads(migration_spec.read_text(encoding="utf-8"))
    assert frozen["type"] == "data_root_migration"
    assert frozen["destination_root"] == str(destination)


def test_data_root_migration_rejects_overlap_and_support_rejects_raw_frames(client: Any, app_settings: Any) -> None:
    response = client.post("/api/v1/support-bundles", json={"include_raw_frames": True})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "RAW_FRAMES_NOT_SUPPORTED"

    response = client.post(
        "/api/v1/system/data-root-migrations",
        json={"destination": str(app_settings.data_root.parent)},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DATA_ROOT_DESTINATION_OVERLAP"
