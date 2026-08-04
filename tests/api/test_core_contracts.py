from __future__ import annotations


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
