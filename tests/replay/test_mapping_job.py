from __future__ import annotations

import time


def test_replay_mapping_worker_publishes_unscaled_map(client):
    project = client.post("/api/v1/projects", json={"name": "Mapping worker"}).json()
    project_id = project["project_id"]
    assert client.post("/api/v1/camera/connect", json={"project_id": project_id, "adapter": "replay", "device_id": "replay:synthetic"}).status_code == 200
    capture = client.post(f"/api/v1/projects/{project_id}/capture-sets", json={"name": "Replay", "source": "replay"}).json()
    batch = client.post(f"/api/v1/projects/{project_id}/capture-sets/{capture['capture_set_id']}/frames:capture", json={"count": 3}).json()
    mapping = client.post(f"/api/v1/projects/{project_id}/maps", json={"capture_set_id": capture["capture_set_id"], "capture_set_revision": batch["capture_set"]["revision"], "compute_profile": "auto"}).json()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{mapping['job_id']}").json()
        if job["state"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert job["state"] == "completed", job
    scene_map = client.get(f"/api/v1/projects/{project_id}/maps/{mapping['map_id']}").json()
    assert scene_map["coordinate_frame"] == "M0"
    assert scene_map["units"] == "arbitrary"
