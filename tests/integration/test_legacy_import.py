from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import select

from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.adapters.persistence.database import Database, JobRecord, ResourceRecord
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.services import Catalog
from spatial_probe_atlas.services.legacy_import import build_legacy_import, publish_legacy_import


POINTS = [
    [-0.005, 0.0, 0.0],
    [-0.01475, -0.04035, 0.04518],
    [-0.02373, 0.04438, 0.03497],
    [-0.00672, -0.00053, -0.05909],
    [-0.01971, 0.03488, -0.02480],
]


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _write_png(path: Path) -> None:
    import cv2

    image = np.zeros((24, 32, 3), dtype=np.uint8)
    image[:, :, 0] = np.arange(32, dtype=np.uint8)
    image[:, :, 1] = 80
    image[:, :, 2] = 180
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), image)


def _legacy_fixture(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "config.json").write_text(
        json.dumps(
            {
                "project_name": "Legacy phantom",
                "SFM_SCALE": 1.25,
                "blob_params": {
                    "minThreshold": 50.0,
                    "maxThreshold": 180.0,
                    "thresholdStep": 10.0,
                    "filterByArea": True,
                    "minArea": 25.0,
                    "maxArea": 900.0,
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "probe_calibration.json").write_text(
        json.dumps({"dot_positions": POINTS, "final_error": 0.72}),
        encoding="utf-8",
    )
    (root / "aruco_board.json").write_text(
        json.dumps(
            {
                "units": "metres",
                "aruco": {"dictionary": "DICT_4X4_50", "marker_ids": [8, 9], "anchor_id": 9, "marker_size_m": 0.02},
                "board": {
                    "markers": {
                        "8": [[0.0, 0.0, 0.0], [0.02, 0.0, 0.0], [0.02, -0.02, 0.0], [0.0, -0.02, 0.0]],
                        "9": [[0.03, 0.0, 0.0], [0.05, 0.0, 0.0], [0.05, -0.02, 0.0], [0.03, -0.02, 0.0]],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    image = root / "uploads" / "sfm" / "frame-000.png"
    _write_png(image)
    (image.parent / "manifest.json").write_text(
        json.dumps({image.name: [[100.0, 0.0, 16.0], [0.0, 100.0, 12.0], [0.0, 0.0, 1.0]]}),
        encoding="utf-8",
    )
    points = [(x * 0.01, y * 0.01, z * 0.01, 20 + x * 20, 30 + y * 20, 40 + z * 20) for x in range(3) for y in range(3) for z in range(2)]
    ply = [
        "ply",
        "format ascii 1.0",
        f"element vertex {len(points)}",
        "property float x",
        "property float y",
        "property float z",
        "property uchar red",
        "property uchar green",
        "property uchar blue",
        "end_header",
        *[" ".join(map(str, item)) for item in points],
    ]
    (root / "sfm_map").mkdir()
    (root / "sfm_map" / "pointcloud.ply").write_text("\n".join(ply) + "\n", encoding="ascii")
    # The PLY must win over the prototype's flat viewer JSON.
    (root / "sfm_map" / "pointcloud.json").write_text(
        json.dumps({"xyz": [0, 0, 0, 1, 0, 0, 0, 1, 0], "rgb": [255, 0, 0, 0, 255, 0, 0, 0, 255]}),
        encoding="utf-8",
    )
    (root / "notes.txt").write_text("operator note must survive\n", encoding="utf-8")
    (root / "session" ).mkdir()
    (root / "session" / "path.json").write_text(json.dumps({"points": [[1, 2, 3]]}), encoding="utf-8")


def _run(store: ArtifactStore, job_id: str, source: Path, project_id: str, *, confirm: bool) -> dict[str, Any]:
    return build_legacy_import(
        store,
        job_id=job_id,
        project_id=project_id,
        source_directory=str(source),
        requested_project_name=None,
        confirm_defaulted_probe_settings=confirm,
        progress=lambda *_: None,
        cancelled=lambda: False,
    )


def test_probe_defaults_require_explicit_confirmation_and_source_is_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "selected-legacy"
    _legacy_fixture(source)
    before = _hashes(source)
    data_root = tmp_path / "data"
    data_root.mkdir()
    store = ArtifactStore(data_root)
    store.staging.mkdir()
    store.projects.mkdir()

    with pytest.raises(AppError) as captured:
        _run(store, "job-defaults", source, "ef45a282-8158-4a85-a9c7-1d4543307ad0", confirm=False)

    assert captured.value.code == "LEGACY_PROBE_DEFAULT_CONFIRMATION_REQUIRED"
    assert "blob_detector.minRepeatability" in captured.value.details["defaulted_fields"]
    assert "probe.t_marker_tip" in captured.value.details["defaulted_fields"]
    assert _hashes(source) == before
    assert not any(store.projects.iterdir())


def test_import_builds_current_artifacts_publishes_atomically_and_never_invents_sessions(tmp_path: Path) -> None:
    source = tmp_path / "selected-legacy"
    _legacy_fixture(source)
    before = _hashes(source)
    data_root = tmp_path / "data"
    data_root.mkdir()
    store = ArtifactStore(data_root)
    store.staging.mkdir()
    store.projects.mkdir()
    database = Database(f"sqlite:///{(data_root / 'app.db').as_posix()}")
    database.migrate()
    catalog = Catalog(database, store)
    project_id = "aeb92df4-b36e-43c9-b71c-f8ee6f614ec2"
    job = catalog.create_job(
        project_id=None,
        owner_id=project_id,
        type="legacy_import",
        spec={"project_id": project_id, "source_directory": str(source), "confirm_defaulted_probe_settings": True},
    )

    built = _run(store, job["job_id"], source, project_id, confirm=True)
    published = publish_legacy_import(database, store, job["job_id"], built)

    assert published["project"]["id"] == project_id
    assert published["project"]["name"] == "Legacy phantom"
    assert published["project"]["active_map_id"]
    assert published["project"]["active_probe_calibration_id"]
    assert published["project"]["active_registration_id"] is None
    assert _hashes(source) == before
    final = store.projects / project_id
    assert final.is_dir()
    assert not (store.staging / job["job_id"] / "legacy-import" / "project").exists()

    report = json.loads((final / "migration-report.json").read_text(encoding="utf-8"))
    assert report["source"]["source_path_redacted"] is True
    assert "path" not in report["source"]
    assert report["map"]["source_type"] == "ply"
    assert report["map"]["point_count"] == 18
    assert report["probe_calibration"]["defaults_confirmed"] is True
    assert report["registration"] == {
        "registration_id": report["registration"]["registration_id"],
        "has_similarity": True,
        "has_board_definition": True,
        "validation_status": "not_run",
    }
    assert report["sessions_created"] == 0
    assert report["paint_paths_created"] == 0
    unknown = {item["source_relative_path"]: item for item in report["unknown_files"]}
    assert "notes.txt" in unknown
    assert "session/path.json" in unknown
    for value in unknown.values():
        preserved = final / value["preserved_relative_uri"]
        assert hashlib.sha256(preserved.read_bytes()).hexdigest() == value["sha256"]

    with database.session() as session:
        resources = list(session.scalars(select(ResourceRecord).where(ResourceRecord.project_id == project_id)))
        kinds = [item.kind for item in resources]
        persisted_job = session.get(JobRecord, job["job_id"])
    assert "capture_set" in kinds
    assert "capture_frame" in kinds
    assert "scene_map" in kinds
    assert "probe_calibration" in kinds
    assert "registration" in kinds
    assert not {"session", "painted_point", "painted_path"}.intersection(kinds)
    assert persisted_job is not None and persisted_job.project_id == project_id

    scene_map = catalog.get_resource(project_id, "scene_map", published["project"]["active_map_id"])
    ply_path = data_root / scene_map["ply"]["relative_uri"]
    manifest_path = data_root / scene_map["manifest"]["relative_uri"]
    assert hashlib.sha256(ply_path.read_bytes()).hexdigest() == scene_map["ply"]["sha256"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["format"] == "spatial-probe-atlas-octree"
    assert sum(manifest["tiles"][tile]["point_count"] for tile in manifest["root_tiles"]) == 18
    for tile in manifest["tiles"].values():
        tile_path = data_root / tile["uri"]
        assert hashlib.sha256(tile_path.read_bytes()).hexdigest() == tile["sha256"]

    calibration = catalog.get_resource(project_id, "probe_calibration", published["project"]["active_probe_calibration_id"])
    assert calibration["probe"]["marker_points_m"] == POINTS
    assert calibration["blob_detector"]["minThreshold"] == 50.0
    assert calibration["blob_detector"]["minRepeatability"] == 2
    assert calibration["migration_provenance"]["defaults_confirmed"] is True
    registration = next(item for item in resources if item.kind == "registration")
    assert registration.state == "draft"
    assert registration.payload["similarity_s_w_m0"]["scale"] == 1.25
    assert registration.payload["validation_status"] == "not_run"

    recovered = publish_legacy_import(database, store, job["job_id"], built)
    assert recovered["publication_recovered"] is True


def test_linked_content_is_rejected_instead_of_followed(tmp_path: Path) -> None:
    source = tmp_path / "selected-legacy"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("not selected", encoding="utf-8")
    link = source / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links")
    data_root = tmp_path / "data"
    data_root.mkdir()
    store = ArtifactStore(data_root)
    store.staging.mkdir()
    store.projects.mkdir()

    with pytest.raises(AppError) as captured:
        _run(store, "job-link", source, "58111fe0-17b4-4ccf-8133-cc05d2872bc6", confirm=True)

    assert captured.value.code == "LEGACY_SOURCE_LINK_NOT_ALLOWED"
    assert outside.read_text(encoding="utf-8") == "not selected"


def test_spawned_worker_uses_immutable_spec_and_structured_progress(tmp_path: Path) -> None:
    source = tmp_path / "selected-legacy"
    _legacy_fixture(source)
    before = _hashes(source)
    data_root = tmp_path / "data"
    data_root.mkdir()
    store = ArtifactStore(data_root)
    store.staging.mkdir()
    store.projects.mkdir()
    job_id = "7d40b442-d328-43cf-8586-cc83c06ef62d"
    worker_dir = store.staging / job_id
    worker_dir.mkdir()
    spec_path = worker_dir / "worker-spec.json"
    progress_path = worker_dir / "worker-progress.json"
    result_path = worker_dir / "worker-result.json"
    cancel_path = worker_dir / "cancel.requested"
    spec_path.write_text(json.dumps({
        "schema_version": 1,
        "type": "legacy_import",
        "data_root": str(data_root),
        "job_id": job_id,
        "project_id": "6db8a526-019f-47ef-98a3-51f736270ad1",
        "source_directory": str(source),
        "requested_project_name": "Spawned legacy import",
        "confirm_defaulted_probe_settings": True,
        "progress_file": str(progress_path),
        "result_file": str(result_path),
        "cancel_file": str(cancel_path),
    }), encoding="utf-8")
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[2] / "backend" / "src")
    environment["PYTHONPATH"] = os.pathsep.join([source_root, environment.get("PYTHONPATH", "")])

    completed = subprocess.run(
        [sys.executable, "-m", "spatial_probe_atlas.jobs.legacy_worker", str(spec_path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(progress_path.read_text(encoding="utf-8"))["stage"] == "ready_to_publish"
    result = json.loads(result_path.read_text(encoding="utf-8"))["result"]
    assert result["project_name"] == "Spawned legacy import"
    assert (data_root / result["staged_project_relative"] / "migration-report.json").is_file()
    assert _hashes(source) == before
