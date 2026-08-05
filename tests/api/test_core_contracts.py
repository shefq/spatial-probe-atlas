from __future__ import annotations

import json
from pathlib import Path


def test_system_and_security_contracts(client):
    assert client.get("/health/live").status_code == 200
    unknown = client.get("/api/v1/does-not-exist")
    assert unknown.status_code == 404
    assert unknown.headers["content-type"].startswith("application/json")
    assert unknown.json()["error"]["code"] == "API_ROUTE_NOT_FOUND"

    rejected = client.get("/api/v1/health/ready", headers={"Host": "example.com:8765"})
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "HOST_NOT_ALLOWED"
    rejected_origin = client.get("/api/v1/health/ready", headers={"Origin": "http://example.com:8765"})
    assert rejected_origin.status_code == 403

    capabilities = client.get("/api/v1/system/capabilities").json()
    assert capabilities["app_version"] == "1.0.0"
    assert capabilities["compute_state"] == "cpu_only"
    assert capabilities["effective_compute_profile"] == "cpu_sift_v1"
    assert capabilities["record3d_state"] in {"available", "not_available"}

    resources = client.get("/api/v1/system/resources").json()
    assert resources["disk_free_bytes"] >= 0
    assert resources["project_size_bytes"] == 0

    settings = client.get("/api/v1/settings").json()
    assert settings["continue_live_in_background"] is False
    assert settings["log_level"] == "INFO"
    updated = client.patch("/api/v1/settings", json={"display_units": "m", "continue_live_in_background": True, "log_level": "WARNING"})
    assert updated.status_code == 200
    assert updated.json()["continue_live_in_background"] is True

    diagnostic = client.post("/api/v1/system/diagnostics").json()
    assert diagnostic["checks"]
    assert all({"key", "name", "state", "detail", "checked_at"} <= set(item) for item in diagnostic["checks"])
    assert {item["state"] for item in diagnostic["checks"]} <= {"pass", "warn", "fail", "skip", "not_available"}

    camera = client.get("/api/v1/camera/status").json()
    assert camera["state"] == "disconnected"
    assert camera["depth_aligned"] is False


def test_project_idempotency_and_validation(client):
    headers = {"Idempotency-Key": "same-project-request"}
    first = client.post("/api/v1/projects", json={"name": "Atlas study"}, headers=headers)
    second = client.post("/api/v1/projects", json={"name": "Ignored retry name"}, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["project_id"] == second.json()["project_id"]
    assert client.post("/api/v1/projects", json={"name": "   "}).status_code == 422


def test_frame_image_route_uses_registered_camera_names(client):
    container = client.app.state.container
    project = container.catalog.create_project("Frame image route")
    project_id = project["project_id"]

    capture_set = container.catalog.create_resource(
        project_id,
        "capture_set",
        state="draft",
        name="Capture set",
        payload={"source": "record3d", "frame_count": 1, "accepted_frame_count": 1, "size_bytes": 0},
    )
    capture_set_id = capture_set["capture_set_id"]

    rgb_path = container.artifacts.project_path(project_id, Path("captures") / capture_set_id / "frames" / "frame-left.rgb8")
    rgb_artifact = container.artifacts.atomic_write_bytes(rgb_path, bytes([255, 0, 0]))
    container.catalog.create_resource(
        project_id,
        "capture_frame",
        state="accepted",
        name="capture-left.png",
        parent_id=capture_set_id,
        payload={
            "sequence": 0,
            "device_timestamp_ns": 1,
            "width": 1,
            "height": 1,
            "intrinsic_matrix": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            "intrinsics_source": "import",
            "rgb_artifact": rgb_artifact,
            "included": True,
        },
    )

    manifest_path = container.artifacts.project_path(project_id, Path("maps") / "map-image-route" / "manifest.json")
    manifest = container.artifacts.atomic_write_json(
        manifest_path,
        {
            "registered_cameras": [{"id": "1", "name": "capture-left.png", "position": [0.0, 0.0, 0.0]}],
            "coordinate_frame": "M0",
            "units": "arbitrary",
        },
    )
    scene_map = container.catalog.create_resource(
        project_id,
        "scene_map",
        state="ready_metric",
        name="Map",
        payload={"manifest": manifest, "coordinate_frame": "M0", "units": "arbitrary"},
    )
    client.post(f"/api/v1/projects/{project_id}/maps/{scene_map['map_id']}/activate")

    response = client.get(f"/api/v1/projects/{project_id}/frames/capture-left.png/image")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert response.content


def test_map_manifest_includes_saved_user_transform(client):
    container = client.app.state.container
    project = container.catalog.create_project("Manifest transform")
    project_id = project["project_id"]

    manifest_path = container.artifacts.project_path(project_id, Path("maps") / "map-transform" / "manifest.json")
    manifest = container.artifacts.atomic_write_json(
        manifest_path,
        {
            "schema_version": "1.0.0",
            "map_id": "map-transform",
            "coordinate_frame": "M0",
            "units": "arbitrary",
            "root_tiles": [],
            "tiles": {},
        },
    )
    scene_map = container.catalog.create_resource(
        project_id,
        "scene_map",
        state="ready_metric",
        name="Map",
        payload={"manifest": manifest, "coordinate_frame": "M0", "units": "arbitrary"},
    )

    transform = {"position": [1.0, 2.0, 3.0], "quaternion": [0.0, 0.0, 0.0, 1.0], "scale": 1.25}
    save = client.post(f"/api/v1/projects/{project_id}/maps/{scene_map['map_id']}/transform", json=transform)
    assert save.status_code == 200
    assert save.json()["user_transform"] == transform

    response = client.get(f"/api/v1/projects/{project_id}/maps/{scene_map['map_id']}/point-cloud/manifest")
    assert response.status_code == 200
    assert response.json()["userTransform"] == transform
