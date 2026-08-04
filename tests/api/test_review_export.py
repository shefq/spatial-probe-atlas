from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from spatial_probe_atlas.main import create_app
from spatial_probe_atlas.services.review_export import run_session_export
from spatial_probe_atlas.settings import Settings


@pytest.fixture()
def review_app(tmp_path: Path) -> Iterator[tuple[TestClient, Any, dict[str, str]]]:
    settings = Settings(data_root=tmp_path / "data", frontend_dist=None, disk_reserve_bytes=0, allow_test_host=True)
    app = create_app(settings, acquire_lock=False)
    with TestClient(app) as client:
        catalog = app.state.container.catalog
        project = catalog.create_project("Review contract")
        project_id = project["project_id"]
        scene_map = catalog.create_resource(
            project_id,
            "scene_map",
            state="active",
            payload={"manifest": {"sha256": "a" * 64}, "units": "m", "coordinate_frame": "W"},
        )
        calibration = catalog.create_resource(
            project_id,
            "probe_calibration",
            state="active",
            payload={"artifact": {"sha256": "b" * 64}, "units": "m"},
        )
        registration = catalog.create_resource(
            project_id,
            "registration",
            state="active",
            payload={"scale": 1.0, "source_frame": "M0", "destination_frame": "W"},
        )
        session = catalog.create_resource(
            project_id,
            "session",
            state="finalized",
            name="Frozen review",
            payload={
                "map_id": scene_map["map_id"],
                "map_revision": scene_map["revision"],
                "probe_calibration_id": calibration["probe_calibration_id"],
                "probe_calibration_revision": calibration["revision"],
                "registration_id": registration["registration_id"],
                "registration_revision": registration["revision"],
                "started_at": "2026-08-04T10:00:00Z",
                "ended_at": "2026-08-04T10:05:00Z",
                "frame_count": 125,
            },
        )
        session_id = session["session_id"]
        records = _seed_records(catalog, project_id, session_id)
        yield client, app.state.container, {
            "project_id": project_id,
            "session_id": session_id,
            "map_id": scene_map["map_id"],
            "calibration_id": calibration["probe_calibration_id"],
            "registration_id": registration["registration_id"],
            **records,
        }


def _seed_records(catalog: Any, project_id: str, session_id: str) -> dict[str, str]:
    point_good = catalog.create_resource(
        project_id,
        "painted_point",
        state="committed",
        parent_id=session_id,
        payload={
            "timestamp": "2026-08-04T10:00:30Z",
            "position_w_m": [0.01, 0.02, 0.03],
            "quality": "good",
            "note": "first",
            "coordinate_frame": "W",
            "units": "m",
        },
    )
    path_warning = catalog.create_resource(
        project_id,
        "painted_path",
        state="committed",
        parent_id=session_id,
        payload={
            "timestamp": "2026-08-04T10:01:00Z",
            "started_at": "2026-08-04T10:01:00Z",
            "ended_at": "2026-08-04T10:01:02Z",
            "quality": "warning",
            "note": "path",
            "coordinate_frame": "W",
            "units": "m",
            "length_m": 0.02,
            "samples": [
                {"timestamp": "2026-08-04T10:01:00Z", "position_w_m": [0.0, 0.0, 0.0], "quality": "warning"},
                {"timestamp": "2026-08-04T10:01:01Z", "position_w_m": [0.01, 0.0, 0.0], "quality": "warning"},
                {"timestamp": "2026-08-04T10:01:02Z", "position_w_m": [0.02, 0.0, 0.0], "quality": "warning"},
            ],
        },
    )
    point_low = catalog.create_resource(
        project_id,
        "painted_point",
        state="flagged_low_quality",
        parent_id=session_id,
        payload={
            "timestamp": "2026-08-04T10:02:00Z",
            "position_w_m": [0.05, 0.04, 0.03],
            "quality": "flagged_low_quality",
            "note": "override",
            "coordinate_frame": "W",
            "units": "m",
        },
    )
    point_deleted = catalog.create_resource(
        project_id,
        "painted_point",
        state="committed",
        parent_id=session_id,
        payload={
            "timestamp": "2026-08-04T10:03:00Z",
            "position_w_m": [0.08, 0.09, 0.10],
            "quality": "good",
            "note": "deleted",
            "coordinate_frame": "W",
            "units": "m",
        },
    )
    catalog.delete_resource(project_id, "painted_point", point_deleted["point_id"])
    return {
        "point_good_id": point_good["point_id"],
        "path_warning_id": path_warning["path_id"],
        "point_low_id": point_low["point_id"],
        "point_deleted_id": point_deleted["point_id"],
    }


def _url(ids: dict[str, str], suffix: str) -> str:
    return f"/api/v1/projects/{ids['project_id']}/sessions/{ids['session_id']}/{suffix}"


def _wait_for_export(client: TestClient, ids: dict[str, str], created: dict[str, Any]) -> dict[str, Any]:
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{created['job_id']}")
        assert job.status_code == 200
        if job.json()["state"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.02)
    exports = client.get(_url(ids, "exports"))
    assert exports.status_code == 200
    value = next(item for item in exports.json() if item["id"] == created["id"])
    assert value["state"] == "completed", job.json()
    return value


def test_server_filters_cursor_and_total_match_review_contract(review_app: tuple[TestClient, Any, dict[str, str]]) -> None:
    client, _, ids = review_app
    response = client.get(
        _url(ids, "painted-records"),
        params={
            "from": "2026-08-04T10:00:00Z",
            "to": "2026-08-04T10:03:00Z",
            "type": "all",
            "quality": "all",
            "include_deleted": "true",
            "limit": 2,
        },
    )
    assert response.status_code == 200
    first = response.json()
    assert set(first) >= {"items", "next_cursor", "total", "filters"}
    assert first["total"] == 4
    assert len(first["items"]) == 2
    assert first["next_cursor"]

    second_response = client.get(
        _url(ids, "painted-records"),
        params={
            "from": "2026-08-04T10:00:00Z",
            "to": "2026-08-04T10:03:00Z",
            "type": "all",
            "quality": "all",
            "include_deleted": "true",
            "limit": 2,
            "cursor": first["next_cursor"],
        },
    )
    assert second_response.status_code == 200
    second = second_response.json()
    assert second["total"] == 4
    assert len(second["items"]) == 2
    assert second["next_cursor"] is None
    assert [item["id"] for item in first["items"] + second["items"]] == [
        ids["point_good_id"], ids["path_warning_id"], ids["point_low_id"], ids["point_deleted_id"]
    ]

    changed_filter = client.get(
        _url(ids, "painted-records"),
        params={"quality": "good", "include_deleted": "true", "limit": 2, "cursor": first["next_cursor"]},
    )
    assert changed_filter.status_code == 422
    assert changed_filter.json()["error"]["code"] == "REVIEW_CURSOR_INVALID"

    points = client.get(
        _url(ids, "painted-points"),
        params={
            "from": "2026-08-04T10:01:30Z",
            "to": "2026-08-04T10:02:30Z",
            "quality": "low",
            "include_deleted": "false",
        },
    )
    assert points.status_code == 200
    assert points.json()["total"] == 1
    assert [item["id"] for item in points.json()["items"]] == [ids["point_low_id"]]


@pytest.mark.parametrize("method", ["patch", "delete", "restore"])
def test_active_session_rejects_review_mutations(review_app: tuple[TestClient, Any, dict[str, str]], method: str) -> None:
    client, container, ids = review_app
    container.catalog.update_resource(ids["project_id"], "session", ids["session_id"], state="running")
    target = _url(ids, f"painted-points/{ids['point_good_id']}")
    if method == "patch":
        response = client.patch(target, json={"note": "must not persist"})
    elif method == "delete":
        response = client.delete(target)
    else:
        container.catalog.delete_resource(ids["project_id"], "painted_point", ids["point_good_id"])
        response = client.post(f"{target}/restore")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_REVIEW_READ_ONLY"


def test_json_export_freezes_filters_and_download_verifies_checksum(review_app: tuple[TestClient, Any, dict[str, str]]) -> None:
    client, container, ids = review_app
    created_response = client.post(
        _url(ids, "exports"),
        json={
            "format": "json",
            "filters": {
                "type": "point",
                "quality": "low",
                "from": "2026-08-04T10:01:30Z",
                "to": "2026-08-04T10:02:30Z",
                "include_deleted": False,
            },
            "include_deleted": False,
        },
    )
    assert created_response.status_code == 202
    created = created_response.json()
    assert created["filters"] == {
        "type": "point",
        "quality": "low",
        "from": "2026-08-04T10:01:30.000000Z",
        "to": "2026-08-04T10:02:30.000000Z",
        "include_deleted": False,
    }
    completed = _wait_for_export(client, ids, created)
    download = client.get(_url(ids, f"exports/{created['id']}/download"))
    assert download.status_code == 200
    assert hashlib.sha256(download.content).hexdigest() == completed["checksum_sha256"]
    assert download.headers["etag"] == f'"{completed["checksum_sha256"]}"'
    document = json.loads(download.content)
    assert document["filters"] == created["filters"]
    assert [point["id"] for point in document["points"]] == [ids["point_low_id"]]
    assert document["paths"] == []
    assert document["coordinate_frame"] == "W"
    assert document["units"] == "m"
    assert set(document["checksums"]) == {
        "filters_sha256", "session_reference_sha256", "points_sha256", "paths_sha256"
    }

    artifact = container.artifacts.root / Path(completed["relative_uri"])
    artifact.write_bytes(b"corrupt")
    rejected = client.get(_url(ids, f"exports/{created['id']}/download"))
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "EXPORT_CHECKSUM_MISMATCH"


def test_csv_manifest_and_orthographic_image_exports_are_self_describing_and_deterministic(
    review_app: tuple[TestClient, Any, dict[str, str]],
) -> None:
    _, container, ids = review_app
    filters = {"type": "all", "quality": "all", "from": None, "to": None, "include_deleted": False}
    spec = {"format": "csv", "filters": filters, "include_deleted": False, "frozen_at": "2026-08-04T11:00:00Z"}
    csv_result = run_session_export(
        container.catalog, container.database, container.artifacts,
        project_id=ids["project_id"], session_id=ids["session_id"], export_id=str(uuid.uuid4()), spec=spec,
    )
    csv_path = container.artifacts.root / Path(csv_result["relative_uri"])
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
    assert rows[0]["row_kind"] == "metadata"
    assert rows[0]["coordinate_frame"] == "W" and rows[0]["units"] == "m"
    assert {row["record_type"] for row in rows[1:]} == {"point", "path"}
    assert sum(row["record_type"] == "path" for row in rows[1:]) == 3
    assert all(row["record_sha256"] for row in rows[1:])

    manifest_result = run_session_export(
        container.catalog, container.database, container.artifacts,
        project_id=ids["project_id"], session_id=ids["session_id"], export_id=str(uuid.uuid4()),
        spec={**spec, "format": "session_manifest"},
    )
    manifest = json.loads((container.artifacts.root / Path(manifest_result["relative_uri"])).read_text(encoding="utf-8"))
    assert manifest["export_type"] == "session_manifest"
    assert manifest["record_counts"] == {"points": 2, "paths": 1, "path_samples": 3, "records": 3}
    refs = manifest["session"]["immutable_revision_refs"]
    assert refs["map"] == {"resource_id": ids["map_id"], "revision": 1, "content_sha256": refs["map"]["content_sha256"], "artifact_sha256": "a" * 64}
    assert refs["probe_calibration"]["resource_id"] == ids["calibration_id"]
    assert refs["registration"]["resource_id"] == ids["registration_id"]
    assert all(len(value) == 64 for value in manifest["checksums"].values())

    first = run_session_export(
        container.catalog, container.database, container.artifacts,
        project_id=ids["project_id"], session_id=ids["session_id"], export_id=str(uuid.uuid4()),
        spec={**spec, "format": "screenshot"},
    )
    second = run_session_export(
        container.catalog, container.database, container.artifacts,
        project_id=ids["project_id"], session_id=ids["session_id"], export_id=str(uuid.uuid4()),
        spec={**spec, "format": "screenshot"},
    )
    first_bytes = (container.artifacts.root / Path(first["relative_uri"])).read_bytes()
    second_bytes = (container.artifacts.root / Path(second["relative_uri"])).read_bytes()
    assert first_bytes == second_bytes
    assert first["rendering"]["renderer"] == "server_orthographic_v1"
    assert first["rendering"]["view"] == "W_XY_top_down"
    assert first["rendering"]["includes_map"] is False
    assert b"not a browser-camera screenshot" in first_bytes

    overlay = run_session_export(
        container.catalog, container.database, container.artifacts,
        project_id=ids["project_id"], session_id=ids["session_id"], export_id=str(uuid.uuid4()),
        spec={**spec, "format": "point_overlay"},
    )
    overlay_bytes = (container.artifacts.root / Path(overlay["relative_uri"])).read_bytes()
    assert overlay["rendering"]["transparent"] is True
    assert overlay_bytes[25] == 6  # PNG colour type 6 is RGBA.
