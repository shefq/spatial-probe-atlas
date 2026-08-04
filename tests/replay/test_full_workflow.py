from __future__ import annotations

import hashlib
import json
import struct
import time
from pathlib import Path
from typing import Any

from spatial_probe_atlas.adapters.persistence.database import ResourceRecord


def _body(response):
    assert response.status_code < 400, response.text
    if not response.content:
        return None
    return response.json() if response.headers.get("content-type", "").startswith("application/json") else response.content


def _wait_job(client, job_id: str, timeout: float = 25.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _body(client.get(f"/api/v1/jobs/{job_id}"))
        if value["state"] in {"completed", "failed", "cancelled", "interrupted", "recoverable"}:
            return value
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def _receive_type(websocket, expected: str, attempts: int = 100) -> dict[str, Any]:
    seen = []
    for _ in range(attempts):
        value = websocket.receive_json()
        seen.append(value.get("type"))
        if value.get("type") == expected:
            return value
    raise AssertionError(f"did not receive {expected}; saw {seen}")


def _metric_project(client) -> tuple[str, str, str, str]:
    project_id = _body(client.post("/api/v1/projects", json={"name": "Replay atlas"}))["project_id"]
    _body(client.post("/api/v1/camera/connect", json={"project_id": project_id, "adapter": "replay", "device_id": "replay:synthetic"}))
    status = _body(client.get("/api/v1/camera/status"))
    assert status["depth_aligned"] is True and len(status["intrinsic_matrix"]) == 9

    capture = _body(client.post(f"/api/v1/projects/{project_id}/capture-sets", json={"name": "Replay capture", "source": "replay"}))
    capture_id = capture["capture_set_id"]
    captured = _body(client.post(f"/api/v1/projects/{project_id}/capture-sets/{capture_id}/frames:capture", json={"count": 3}))
    mapping = _body(client.post(f"/api/v1/projects/{project_id}/maps", json={"capture_set_id": capture_id, "capture_set_revision": captured["capture_set"]["revision"], "compute_profile": "auto", "name": "Replay map"}))
    assert mapping["effective_compute_profile"] == "depth_assisted_replay_v1"
    assert _wait_job(client, mapping["job_id"])["state"] == "completed"
    map_id = mapping["map_id"]
    scene_map = _body(client.get(f"/api/v1/projects/{project_id}/maps/{map_id}"))
    assert scene_map["coordinate_frame"] == "M0" and scene_map["units"] == "arbitrary" and scene_map["point_count"] >= 100
    _body(client.post(f"/api/v1/projects/{project_id}/maps/{map_id}/activate"))

    capture = _body(client.post(f"/api/v1/projects/{project_id}/probe-captures", json={"name": "Replay probe", "source": "replay"}))
    _body(client.post(f"/api/v1/projects/{project_id}/probe-captures/{capture['id']}/frames:capture", json={"count": 3}))
    probe = _body(client.post(f"/api/v1/projects/{project_id}/probe-calibrations", json={"probe_capture_id": capture["id"], "name": "Probe v1", "activate": True}))
    assert probe["active"] is True
    probe_id = probe["probe_calibration_id"]

    registration = _body(client.post(f"/api/v1/projects/{project_id}/registrations", json={"name": "Tissue registration", "map_id": map_id, "probe_calibration_id": probe_id}))
    registration_id = registration["registration_id"]
    for index in range(3):
        value = _body(client.post(f"/api/v1/projects/{project_id}/registrations/{registration_id}/observations", json={"source": "current_frame", "label": f"view-{index}"}))
        assert value["observation_count"] == index + 1
    solved = _body(client.post(f"/api/v1/projects/{project_id}/registrations/{registration_id}/solve"))
    assert abs(solved["scale"] - 1.25) < 1e-6
    assert _body(client.post(f"/api/v1/projects/{project_id}/registrations/{registration_id}/validate", json={}))["validation_state"] == "passed"
    assert _body(client.post(f"/api/v1/projects/{project_id}/registrations/{registration_id}/activate"))["active"] is True

    manifest = _body(client.get(f"/api/v1/projects/{project_id}/maps/{map_id}/point-cloud/manifest"))
    assert manifest["coordinate_frame"] == "M0" and manifest["units"] == "arbitrary"
    assert manifest["published_coordinate_frame"] == "W" and manifest["metric_binding"]["registration_id"] == registration_id
    tile = _body(client.get(f"/api/v1/projects/{project_id}/maps/{map_id}/point-cloud/tiles/r"))
    magic, version, flags, count = struct.unpack("<8sHHI", tile[:16])
    assert (magic, version, flags, count) == (b"SPATILE1", 1, 1, manifest["tiles"]["r"]["point_count"])
    return project_id, map_id, probe_id, registration_id


def test_replay_mapping_registration_live_recovery_export_and_clone(client):
    project_id, map_id, probe_id, registration_id = _metric_project(client)
    session = _body(client.post(f"/api/v1/projects/{project_id}/sessions", json={"name": "Live replay", "notes": "test", "compute_profile": "cpu"}, headers={"Idempotency-Key": "session-create"}))
    session_id = session["session_id"]
    assert min(session["map_revision"], session["probe_calibration_revision"], session["registration_revision"]) >= 1
    _body(client.post(f"/api/v1/projects/{project_id}/sessions/{session_id}/start"))

    with client.websocket_connect(f"/ws/v1/projects/{project_id}/sessions/{session_id}/tracking") as websocket:
        _receive_type(websocket, "session.status")
        websocket.send_json({"type": "paint.path.start", "command_id": "path-1", "data": {"sampling": {"mode": "time", "interval_ms": 1}}})
        _receive_type(websocket, "paint.path_started")
        for _ in range(4):
            assert _receive_type(websocket, "tracking.frame")["data"]["tip_w_m"] is not None
    recovered = _body(client.get(f"/api/v1/projects/{project_id}/sessions/{session_id}"))
    assert recovered["state"] == "recoverable" and recovered["active_path"] is None and recovered["path_count"] >= 1
    assert _body(client.post(f"/api/v1/projects/{project_id}/sessions/{session_id}/resume"))["state"] == "running"

    point = _body(client.post(f"/api/v1/projects/{project_id}/sessions/{session_id}/painted-points", json={"command_id": "point-1"}))
    assert point["coordinate_frame"] == "W" and point["units"] == "m"
    _body(client.post(f"/api/v1/projects/{project_id}/sessions/{session_id}/stop"))
    _body(client.post(f"/api/v1/projects/{project_id}/sessions/{session_id}/finalize"))
    export = _body(client.post(f"/api/v1/projects/{project_id}/sessions/{session_id}/exports", json={"format": "csv", "include_deleted": False}))
    assert _wait_job(client, export["job_id"])["state"] == "completed"
    exports = _body(client.get(f"/api/v1/projects/{project_id}/sessions/{session_id}/exports"))
    completed = next(item for item in exports if item["state"] == "completed")
    csv_response = client.get(completed["download_url"])
    assert csv_response.status_code == 200 and "record_type" in csv_response.text and "point" in csv_response.text and "path" in csv_response.text

    clone = _body(client.post(f"/api/v1/projects/{project_id}/clone", json={"name": "Replay atlas clone"}))["project"]
    clone_id = clone["project_id"]
    assert clone["active_map_id"] != map_id and clone["active_probe_calibration_id"] != probe_id and clone["active_registration_id"] != registration_id
    container = client.app.state.container
    for kind in ("capture_frame", "scene_map", "probe_calibration", "registration"):
        for resource in container.catalog.list_resources(clone_id, kind, limit=1000):
            _assert_artifacts(resource, container.artifacts.root)
    with container.database.session() as database_session:
        payload = dict(database_session.get(ResourceRecord, clone["active_probe_calibration_id"]).payload)
    canonical = {key: value for key, value in payload.items() if key not in {"artifact", "checksum"}}
    expected = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert payload["checksum"] == expected


def _assert_artifacts(value: Any, root: Path) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("relative_uri"), str):
            path = root / value["relative_uri"]
            assert path.is_file(), value["relative_uri"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == value["sha256"]
        for item in value.values():
            _assert_artifacts(item, root)
    elif isinstance(value, list):
        for item in value:
            _assert_artifacts(item, root)
