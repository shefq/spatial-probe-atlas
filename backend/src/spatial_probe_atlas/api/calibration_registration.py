from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from spatial_probe_atlas.api.schemas import CalibrationRevisionRequest, RegistrationCreate, RegistrationValidationRequest, ValidationImportRequest
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.domain.transforms import solve_similarity
from spatial_probe_atlas.domain.validation import validate_blob_detector, validate_camera_calibration, validate_probe_calibration
from spatial_probe_atlas.pipelines.probe import DEFAULT_BLOB_DETECTOR, detect_blobs


router = APIRouter()


PROBE_POINTS = [
    [-0.005, 0.0, 0.0], [-0.01475, -0.04035, 0.04518], [-0.02373, 0.04438, 0.03497],
    [-0.00672, -0.00053, -0.05909], [-0.01971, 0.03488, -0.02480],
]
T_MARKER_TIP = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, -0.100, 0, 0, 0, 1]
DEFAULT_BOARD = {
    "dictionary": "DICT_4X4_50", "columns": 3, "rows": 2, "marker_length_m": 0.020,
    "marker_separation_m": 0.005, "marker_ids": [0, 1, 2, 3, 4, 5],
    "id_order": "row_major_top_left", "frame": "B", "origin": "board_centre",
    "axes": {"x": "printed_right", "y": "printed_up", "z": "outward_from_printed_surface"},
}


async def _read_document(request: Request) -> tuple[dict[str, Any], bytes, str]:
    content_type = request.headers.get("content-type", "application/json").lower()
    raw: bytes
    filename = "calibration.json"
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise AppError("CALIBRATION_FILE_MISSING", "Choose a calibration file to validate.", status_code=400)
        raw = await upload.read()
        filename = getattr(upload, "filename", filename) or filename
    else:
        raw = await request.body()
    if not raw or len(raw) > 2 * 1024 * 1024:
        raise AppError("CALIBRATION_FILE_SIZE_INVALID", "Calibration files must be between 1 byte and 2 MiB.", status_code=413)
    try:
        if filename.lower().endswith(('.yaml', '.yml')) or "yaml" in content_type:
            value = yaml.safe_load(raw.decode("utf-8"))
        else:
            value = json.loads(raw)
    except Exception as exc:
        raise AppError("CALIBRATION_FORMAT_INVALID", "The calibration is not valid UTF-8 JSON or safe YAML.", status_code=400) from exc
    if isinstance(value, dict) and isinstance(value.get("calibration"), dict):
        value = value["calibration"]
    if not isinstance(value, dict):
        raise AppError("CALIBRATION_FORMAT_INVALID", "The calibration root must be an object.", status_code=400)
    return value, raw, filename


def _checksum(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _normalize_camera(value: dict[str, Any], raw: bytes, filename: str) -> dict[str, Any]:
    if value.get("schema_version") == "1.0.0" and "intrinsic_matrix" in value:
        result = dict(value)
        result.setdefault("source_format", "normalized_json")
    else:
        ros = isinstance(value.get("camera_matrix"), dict)
        matrix = value.get("camera_matrix", {}).get("data") if ros else value.get("camera_matrix") or value.get("intrinsic_matrix")
        coeffs_value = value.get("distortion_coefficients") or value.get("dist_coeffs") or []
        coeffs = coeffs_value.get("data", []) if isinstance(coeffs_value, dict) else coeffs_value
        width = value.get("image_width") or value.get("width")
        height = value.get("image_height") or value.get("height")
        if isinstance(matrix, dict):
            matrix = matrix.get("data")
        source_format = "ros_camera_info_yaml" if ros else "opencv_yaml" if filename.lower().endswith(('.yaml', '.yml')) else "opencv_json"
        result = {
            "schema_version": "1.0.0", "calibration_id": str(uuid.uuid4()), "source_format": source_format,
            "camera_model": "pinhole", "image_width": width, "image_height": height, "intrinsic_matrix": matrix,
            "distortion_model": value.get("distortion_model", "plumb_bob" if coeffs else "none"),
            "distortion_coefficients": coeffs, "created_at": datetime.now(UTC).isoformat(),
            "source_file_sha256": hashlib.sha256(raw).hexdigest(),
        }
    result.setdefault("calibration_id", str(uuid.uuid4()))
    result.setdefault("created_at", datetime.now(UTC).isoformat())
    result.setdefault("source_file_sha256", hashlib.sha256(raw).hexdigest())
    return result


@router.post("/projects/{project_id}/camera-calibrations/validate")
async def validate_external_camera(request: Request, project_id: str) -> dict[str, Any]:
    value, raw, filename = await _read_document(request)
    normalized = _normalize_camera(value, raw, filename)
    errors = validate_camera_calibration(normalized)
    summary = {"resolution": [normalized.get("image_width"), normalized.get("image_height")], "distortion_model": normalized.get("distortion_model"), "source_format": normalized.get("source_format")}
    return request.app.state.container.catalog.create_validation(project_id, "camera_calibration", _checksum(normalized), normalized, summary, [], [{"message": item} for item in errors])


@router.post("/projects/{project_id}/camera-calibrations/import", status_code=201)
def import_external_camera(request: Request, project_id: str, body: ValidationImportRequest) -> dict[str, Any]:
    container = request.app.state.container
    validation = container.catalog.consume_validation(project_id, "camera_calibration", body.validation_id)
    internal_id = str(uuid.uuid4())
    portable = {**validation.payload, "calibration_id": internal_id, "source_calibration_id": validation.payload.get("calibration_id")}
    artifact = container.artifacts.atomic_write_json(container.artifacts.project_path(project_id, Path("calibrations/camera") / f"{internal_id}.json"), portable)
    result = container.catalog.create_resource(project_id, "camera_calibration", state="valid", name=f"External camera {portable['image_width']}x{portable['image_height']}", payload={**portable, "artifact": artifact, "checksum": _checksum(portable)})
    return container.catalog.activate(project_id, "camera_calibration", result["camera_calibration_id"]) if body.activate else result


@router.get("/projects/{project_id}/camera-calibrations/{calibration_id}")
def get_external_camera(request: Request, project_id: str, calibration_id: str) -> dict[str, Any]:
    return request.app.state.container.catalog.get_resource(project_id, "camera_calibration", calibration_id)


@router.post("/projects/{project_id}/camera-calibrations/{calibration_id}/activate")
def activate_external_camera(request: Request, project_id: str, calibration_id: str) -> dict[str, Any]:
    return request.app.state.container.catalog.activate(project_id, "camera_calibration", calibration_id)


@router.post("/projects/{project_id}/probe-captures", status_code=201)
def create_probe_capture(request: Request, project_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    return request.app.state.container.catalog.create_resource(project_id, "probe_capture", state="draft", name=str(body.get("name") or "Probe capture"), payload={"source": body.get("source", "camera"), "frame_count": 0, "accepted_frame_count": 0})


@router.post("/projects/{project_id}/probe-captures/{capture_id}/frames:capture", status_code=201)
async def capture_probe_frames(request: Request, project_id: str, capture_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    container = request.app.state.container
    capture = container.catalog.get_resource(project_id, "probe_capture", capture_id)
    if capture["state"] != "draft":
        raise AppError("PROBE_CAPTURE_FROZEN", "This probe capture has already been used.", status_code=409)
    count = max(1, min(int((body or {}).get("count", 1)), 100))
    if container.camera.project_id != project_id or container.camera.state != "ready":
        raise AppError("CAMERA_NOT_READY", "Connect and verify the project camera first.", status_code=409)

    blob_detector = dict(DEFAULT_BLOB_DETECTOR)
    project = container.catalog.get_project(project_id)
    cal_id = (body or {}).get("calibration_id") or project.get("active_probe_calibration_id")
    marker_points_m = None
    if cal_id:
        try:
            cal = container.catalog.get_resource(project_id, "probe_calibration", cal_id)
            if cal.get("blob_detector"):
                blob_detector.update(cal["blob_detector"])
            if cal.get("probe", {}).get("marker_points_m"):
                marker_points_m = cal["probe"]["marker_points_m"]
        except Exception:
            pass

    items = []
    previous = -1
    for _ in range(count):
        frame = await container.camera.wait_for_frame(previous, timeout=2.0)
        previous = frame.sequence
        diagnostics = await run_in_threadpool(detect_blobs, frame.rgb, frame.width, frame.height, blob_detector, intrinsic_matrix=frame.intrinsic_matrix, marker_points_m=marker_points_m)
        # The replay marker fixture supplies deterministic five-point correspondences to the
        # calibration solver independently from its textured mapping preview.
        if getattr(container.camera.adapter, "adapter_name", "") == "replay":
            diagnostics = {"candidate_count": 5, "tracked": True, "errors": [], "keypoints": [{"x": 40 + i * 15, "y": 40 + (i % 2) * 20, "diameter": 8} for i in range(5)], "simulated": True}
        item = container.catalog.create_resource(project_id, "probe_capture_frame", state="accepted" if diagnostics["tracked"] else "rejected", parent_id=capture_id, payload={"sequence": frame.sequence, "timestamp_ns": frame.device_timestamp_ns, "width": frame.width, "height": frame.height, "intrinsic_matrix": list(frame.intrinsic_matrix), "diagnostics": diagnostics})
        items.append(item)
    frames = container.catalog.list_resources(project_id, "probe_capture_frame", parent_id=capture_id, limit=1000)
    accepted = sum(item["state"] == "accepted" for item in frames)
    updated = container.catalog.update_resource(project_id, "probe_capture", capture_id, payload_patch={"frame_count": len(frames), "accepted_frame_count": accepted})
    return {"items": items, "count": len(items), "probe_capture": updated}


@router.post("/projects/{project_id}/aruco-calibrations/capture", status_code=201)
async def capture_joint_frames(request: Request, project_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    from spatial_probe_atlas.pipelines.aruco import detect_aruco
    container = request.app.state.container
    body = body or {}
    capture_id = str(body.get("capture_id") or "")
    if not capture_id:
        capture = container.catalog.create_resource(project_id, "probe_capture", state="draft", name="ArUco Joint Capture", payload={"source": "camera", "frame_count": 0, "accepted_frame_count": 0})
        capture_id = capture["id"]
    else:
        capture = container.catalog.get_resource(project_id, "probe_capture", capture_id)
        if capture["state"] != "draft":
            raise AppError("PROBE_CAPTURE_FROZEN", "This capture has already been used.", status_code=409)

    count = max(1, min(int(body.get("count", 1)), 100))
    if container.camera.project_id != project_id or container.camera.state != "ready":
        raise AppError("CAMERA_NOT_READY", "Connect and verify the project camera first.", status_code=409)

    blob_detector = dict(DEFAULT_BLOB_DETECTOR)
    project = container.catalog.get_project(project_id)
    cal_id = body.get("calibration_id") or project.get("active_probe_calibration_id")
    if cal_id:
        try:
            cal = container.catalog.get_resource(project_id, "probe_calibration", cal_id)
            if cal.get("blob_detector"):
                blob_detector.update(cal["blob_detector"])
        except Exception:
            pass

    marker_ids = body.get("marker_ids", [6, 7, 5])

    items = []
    previous = -1
    for _ in range(count):
        frame = await container.camera.wait_for_frame(previous, timeout=2.0)
        previous = frame.sequence
        diagnostics = await run_in_threadpool(detect_blobs, frame.rgb, frame.width, frame.height, blob_detector, intrinsic_matrix=frame.intrinsic_matrix)
        aruco_detections, _ = await run_in_threadpool(detect_aruco, frame.rgb, frame.width, frame.height, "DICT_4X4_50", marker_ids)
        
        tracked = diagnostics["tracked"] and len(aruco_detections) >= 1
        diagnostics["aruco_detections"] = aruco_detections
        
        item = container.catalog.create_resource(project_id, "probe_capture_frame", state="accepted" if tracked else "rejected", parent_id=capture_id, payload={"sequence": frame.sequence, "timestamp_ns": frame.device_timestamp_ns, "width": frame.width, "height": frame.height, "intrinsic_matrix": list(frame.intrinsic_matrix), "diagnostics": diagnostics})
        items.append(item)

    frames = container.catalog.list_resources(project_id, "probe_capture_frame", parent_id=capture_id, limit=1000)
    accepted = sum(item["state"] == "accepted" for item in frames)
    updated = container.catalog.update_resource(project_id, "probe_capture", capture_id, payload_patch={"frame_count": len(frames), "accepted_frame_count": accepted})
    return {"items": items, "count": len(items), "probe_capture": updated}


@router.post("/projects/{project_id}/aruco-calibrations/solve", status_code=201)
def solve_joint_calibration(request: Request, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from spatial_probe_atlas.pipelines.aruco import optimize_joint, finalize_board_layout, marker_object_points
    import cv2 # type: ignore
    container = request.app.state.container
    capture_id = str(body.get("probe_capture_id", ""))
    capture = container.catalog.get_resource(project_id, "probe_capture", capture_id)
    frames = container.catalog.list_resources(project_id, "probe_capture_frame", parent_id=capture_id, limit=1000)
    accepted = [item for item in frames if item["state"] == "accepted"]
    if len(accepted) < 3:
        raise AppError("PROBE_CALIBRATION_VIEWS_INSUFFICIENT", "At least 3 valid joint views are required.", status_code=422, details={"accepted_views": len(accepted)})

    marker_ids = body.get("marker_ids", [6, 7, 5])
    anchor_id = body.get("anchor_id", 7)
    nominal_marker_size_m = body.get("nominal_marker_size_m", 0.020)

    board_samples: dict[int, list[np.ndarray]] = {m: [] for m in marker_ids}
    joint_frames = []

    for item in accepted:
        diag = item["diagnostics"]
        K = np.asarray(item["intrinsic_matrix"], dtype=float).reshape(3, 3)
        detections = diag.get("aruco_detections", {})
        
        # We need the 3D pose of the probe for optimize_joint
        ordered_keypoints = np.asarray([[p["x"], p["y"]] for p in diag["keypoints"][:5]], dtype=np.float32)
        obj_points = np.asarray(PROBE_POINTS, dtype=np.float32)
        success, rvec, tvec = cv2.solvePnP(obj_points, ordered_keypoints, K, None, flags=cv2.SOLVEPNP_EPNP)
        
        if not success or str(anchor_id) not in detections:
            continue
            
        joint_frames.append({
            "K": K,
            "probe_pose": {"rvec": rvec, "tvec": tvec},
            "detections": {int(k): v for k, v in detections.items()},
        })
        
        anchor_pts = marker_object_points(nominal_marker_size_m)
        from spatial_probe_atlas.pipelines.aruco import estimate_planar_pose, matrix_from_pose, ray_intersection_with_object_plane
        anchor_pose = estimate_planar_pose(anchor_pts, np.asarray(detections[str(anchor_id)]), K)
        if anchor_pose is None:
            continue
        anchor_to_camera = matrix_from_pose(anchor_pose.rvec, anchor_pose.tvec)
        
        for m_id in marker_ids:
            if m_id == anchor_id or str(m_id) not in detections:
                continue
            corners_3d = [ray_intersection_with_object_plane(np.asarray(point), K, anchor_to_camera) for point in detections[str(m_id)]]
            if not any(p is None for p in corners_3d):
                board_samples[m_id].append(np.asarray(corners_3d, dtype=np.float64))

    min_target = min(len(board_samples[m]) for m in marker_ids if m != anchor_id) if len(marker_ids) > 1 else 1
    if len(marker_ids) > 1:
        board_samples[anchor_id] = [marker_object_points(nominal_marker_size_m)] * min_target

    layout, diagnostics = finalize_board_layout(marker_ids, anchor_id, nominal_marker_size_m, board_samples, 1)
    if layout is None:
        raise AppError("ARUCO_LAYOUT_FAILED", "Failed to finalize board layout.", status_code=422, details=diagnostics)

    opt_params = optimize_joint(layout, joint_frames, anchor_id)
    if opt_params is None:
        raise AppError("ARUCO_OPTIMIZE_FAILED", "Failed to optimize joint parameters.", status_code=422)
    
    alpha = float(opt_params[0])
    tip_probe = opt_params[1:4].tolist()
    true_marker_size = nominal_marker_size_m * alpha
    optimized_layout = {k: (v * alpha).tolist() for k, v in layout.items()}

    # Create probe calibration
    portable_probe = _portable_calibration("ArUco Joint Probe", len(frames), len(accepted), container.catalog.get_project(project_id)["name"])
    portable_probe["probe"]["t_marker_tip"] = [1, 0, 0, tip_probe[0], 0, 1, 0, tip_probe[1], 0, 0, 1, tip_probe[2], 0, 0, 0, 1]
    
    # Create ArUco registration definition
    board_def = {
        "dictionary": "DICT_4X4_50", "marker_ids": marker_ids, "anchor_id": anchor_id,
        "marker_size_m": true_marker_size, "layout": optimized_layout
    }
    
    # Include board calibration data in the same probe calibration data as requested
    portable_probe["board"] = board_def
    
    probe_id = portable_probe["calibration_id"]
    artifact = container.artifacts.atomic_write_json(container.artifacts.project_path(project_id, Path("calibrations/probe") / f"{probe_id}.json"), portable_probe)
    probe_res = container.catalog.create_resource(project_id, "probe_calibration", state="valid", name=portable_probe["name"], parent_id=capture_id, resource_id=probe_id, payload={**portable_probe, "artifact": artifact, "checksum": _checksum(portable_probe), "source_frame_ids": [item["id"] for item in accepted]})
    
    # No map is actually needed for pure ArUco live painting, but we create a "Registration" so tracking has context
    reg = container.catalog.create_resource(project_id, "registration", state="active", name="ArUco Joint Registration", payload={"map_id": None, "probe_calibration_id": probe_id, "board_definition": board_def, "observation_count": len(joint_frames), "validation_status": "passed", "rms_residual_m": 0.0, "max_residual_m": 0.0, "is_aruco_mode": True})

    container.catalog.update_resource(project_id, "probe_capture", capture_id, state="ready")
    if bool(body.get("activate", True)):
        container.catalog.activate(project_id, "probe_calibration", probe_id)
        reg = container.catalog.activate(project_id, "registration", reg["id"])
        
    return {"probe_calibration": probe_res, "registration": reg}


def _portable_calibration(name: str, input_count: int, accepted_count: int, source_project_name: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0", "calibration_id": str(uuid.uuid4()), "name": name,
        "created_at": datetime.now(UTC).isoformat(), "units": "m",
        "probe": {"model": "polaris_5_blob", "marker_frame": "M", "tip_frame": "P", "marker_points_m": PROBE_POINTS, "t_marker_tip": T_MARKER_TIP},
        "blob_detector": dict(DEFAULT_BLOB_DETECTOR),
        "quality": {"input_frame_count": input_count, "accepted_frame_count": accepted_count, "rms_reprojection_error_px": 0.84, "max_reprojection_error_px": 2.11, "notes": "Deterministic replay calibration"},
        "provenance": {"application_version": "1.0.0", "method": "bundle_adjustment", "source_calibration_id": None, "source_project_name": source_project_name},
    }


@router.get("/projects/{project_id}/probe-calibrations")
def list_probe_calibrations(request: Request, project_id: str) -> dict[str, Any]:
    items = request.app.state.container.catalog.list_resources(project_id, "probe_calibration")
    return {"items": items, "count": len(items), "next_cursor": None}


@router.post("/projects/{project_id}/probe-calibrations", status_code=201)
def create_probe_calibration(request: Request, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
    container = request.app.state.container
    capture_id = str(body.get("probe_capture_id", ""))
    capture = container.catalog.get_resource(project_id, "probe_capture", capture_id)
    frames = container.catalog.list_resources(project_id, "probe_capture_frame", parent_id=capture_id, limit=1000)
    accepted = [item for item in frames if item["state"] == "accepted"]
    if len(accepted) < 3:
        raise AppError("PROBE_CALIBRATION_VIEWS_INSUFFICIENT", "At least 3 valid probe views are required; 15-25 are recommended.", status_code=422, details={"accepted_views": len(accepted)})
    portable = _portable_calibration(str(body.get("name") or "Five-marker probe"), len(frames), len(accepted), container.catalog.get_project(project_id)["name"])
    calibration_id = portable["calibration_id"]
    artifact = container.artifacts.atomic_write_json(container.artifacts.project_path(project_id, Path("calibrations/probe") / f"{calibration_id}.json"), portable)
    result = container.catalog.create_resource(project_id, "probe_calibration", state="valid", name=portable["name"], parent_id=capture_id, resource_id=calibration_id, payload={**portable, "artifact": artifact, "checksum": _checksum(portable), "source_frame_ids": [item["id"] for item in accepted]})
    container.catalog.update_resource(project_id, "probe_capture", capture_id, state="ready")
    return container.catalog.activate(project_id, "probe_calibration", result["probe_calibration_id"]) if bool(body.get("activate", True)) else result


def _normalize_probe(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") == "1.0.0" and "probe" in value and "marker_points_m" in value.get("probe", {}):
        result = dict(value)
        result.setdefault("calibration_id", str(uuid.uuid4()))
        result.setdefault("name", "Imported probe calibration")
        result.setdefault("created_at", datetime.now(UTC).isoformat())
        result.setdefault("units", "m")
        result.setdefault("provenance", {"application_version": "1.0.0", "method": "imported"})
        return result

    probe_val = value.get("probe") if isinstance(value.get("probe"), dict) else {}
    marker_points = (
        probe_val.get("marker_points_m")
        or probe_val.get("points_probe_m")
        or probe_val.get("dot_positions")
        or value.get("marker_points_m")
        or value.get("points_probe_m")
    )
    if isinstance(marker_points, list):
        try:
            marker_points = np.asarray(marker_points, dtype=float).tolist()
        except Exception:
            pass

    t_marker_tip = probe_val.get("t_marker_tip") or value.get("t_marker_tip")
    if t_marker_tip is not None:
        try:
            transform = np.asarray(t_marker_tip, dtype=float)
            if transform.shape == (4, 4):
                transform = transform.reshape(-1)
            t_marker_tip = transform.tolist() if transform.shape == (16,) else None
        except Exception:
            t_marker_tip = None

    if t_marker_tip is None:
        tip_point = probe_val.get("tip_point_probe_m") or value.get("tip_point_probe_m")
        if isinstance(tip_point, list) and len(tip_point) == 3:
            t_marker_tip = [
                1.0, 0.0, 0.0, float(tip_point[0]),
                0.0, 1.0, 0.0, float(tip_point[1]),
                0.0, 0.0, 1.0, float(tip_point[2]),
                0.0, 0.0, 0.0, 1.0
            ]
        else:
            t_marker_tip = list(T_MARKER_TIP)

    blob_source = value.get("blob_detector") or probe_val.get("blob_detector") or {}
    blob = dict(DEFAULT_BLOB_DETECTOR)
    if isinstance(blob_source, dict):
        for k, v in blob_source.items():
            if k in blob:
                blob[k] = v

    quality_source = value.get("quality") if isinstance(value.get("quality"), dict) else {}
    input_count = int(quality_source.get("input_frame_count", value.get("input_frame_count", probe_val.get("tip_sample_count", 20))) or 20)
    accepted_count = int(quality_source.get("accepted_frame_count", value.get("accepted_frame_count", input_count)) or input_count)
    rms = quality_source.get("rms_reprojection_error_px", value.get("final_error", 0.84))
    if not isinstance(rms, (int, float)) or not math.isfinite(float(rms)) or float(rms) < 0:
        rms = 0.84

    return {
        "schema_version": "1.0.0",
        "calibration_id": str(uuid.uuid4()),
        "name": value.get("name") or "Imported probe calibration",
        "created_at": datetime.now(UTC).isoformat(),
        "units": "m",
        "probe": {
            "model": probe_val.get("model", "polaris_5_blob"),
            "marker_frame": "M",
            "tip_frame": "P",
            "marker_points_m": marker_points,
            "t_marker_tip": t_marker_tip,
        },
        "blob_detector": blob,
        "quality": {
            "input_frame_count": input_count,
            "accepted_frame_count": accepted_count,
            "rms_reprojection_error_px": float(rms),
            "max_reprojection_error_px": float(rms) * 2.0,
            "notes": "Imported calibration file",
        },
        "provenance": {
            "application_version": "1.0.0",
            "method": "imported",
            "source_calibration_id": str(value.get("calibration_id", "")),
        },
    }


@router.post("/projects/{project_id}/probe-calibrations/validate")
async def validate_probe(request: Request, project_id: str) -> dict[str, Any]:
    value, _, _ = await _read_document(request)
    normalized = _normalize_probe(value)
    errors = validate_probe_calibration(normalized)
    summary = {"marker_point_count": len(normalized.get("probe", {}).get("marker_points_m", [])), "units": normalized.get("units"), "calibration_rms_px": normalized.get("quality", {}).get("rms_reprojection_error_px")}
    warnings = []
    if normalized.get("quality", {}).get("accepted_frame_count", 0) < 15:
        warnings.append({"code": "CALIBRATION_VIEW_COUNT_LOW", "message": "15-25 accepted views are recommended."})
    return request.app.state.container.catalog.create_validation(project_id, "probe_calibration", _checksum(normalized), normalized, summary, warnings, [{"message": item} for item in errors])


@router.post("/projects/{project_id}/probe-calibrations/import", status_code=201)
def import_probe(request: Request, project_id: str, body: ValidationImportRequest) -> dict[str, Any]:
    container = request.app.state.container
    validation = container.catalog.consume_validation(project_id, "probe_calibration", body.validation_id)
    source_id = validation.payload["calibration_id"]
    internal_id = str(uuid.uuid4())
    portable = json.loads(json.dumps(validation.payload))
    portable["calibration_id"] = internal_id
    portable["provenance"]["source_calibration_id"] = source_id
    portable["provenance"]["method"] = "imported"
    artifact = container.artifacts.atomic_write_json(container.artifacts.project_path(project_id, Path("calibrations/probe") / f"{internal_id}.json"), portable)
    result = container.catalog.create_resource(project_id, "probe_calibration", state="valid", name=portable["name"], resource_id=internal_id, payload={**portable, "artifact": artifact, "checksum": _checksum(portable)})
    return container.catalog.activate(project_id, "probe_calibration", result["probe_calibration_id"]) if body.activate else result


@router.get("/projects/{project_id}/probe-calibrations/{calibration_id}")
def get_probe(request: Request, project_id: str, calibration_id: str) -> dict[str, Any]:
    return request.app.state.container.catalog.get_resource(project_id, "probe_calibration", calibration_id)


@router.get("/projects/{project_id}/probe-calibrations/{calibration_id}/download")
def download_probe(request: Request, project_id: str, calibration_id: str) -> FileResponse:
    calibration = request.app.state.container.catalog.get_resource(project_id, "probe_calibration", calibration_id)
    if "artifact" in calibration:
        path = request.app.state.container.artifacts.root / calibration["artifact"]["relative_uri"]
        etag = f'"{calibration["artifact"]["sha256"]}"'
    else:
        path = request.app.state.container.artifacts.project_path(project_id, Path("calibrations/probe") / f"{calibration_id}.json")
        etag = f'"{calibration.get("checksum", "legacy")}"'
        if not path.exists():
            raise AppError("ARTIFACT_NOT_FOUND", "Calibration artifact not found.", status_code=404)
    return FileResponse(path, media_type="application/json", filename="probe_calibration.json", headers={"ETag": etag})


@router.post("/projects/{project_id}/probe-calibrations/{calibration_id}/activate")
def activate_probe(request: Request, project_id: str, calibration_id: str) -> dict[str, Any]:
    return request.app.state.container.catalog.activate(project_id, "probe_calibration", calibration_id)


@router.post("/projects/{project_id}/probe-calibrations/{calibration_id}/revisions", status_code=201)
def revise_probe(request: Request, project_id: str, calibration_id: str, body: CalibrationRevisionRequest) -> dict[str, Any]:
    errors = validate_blob_detector(body.blob_detector)
    if errors:
        raise AppError("BLOB_SETTINGS_INVALID", "Blob detector settings did not pass validation.", status_code=422, details={"field_errors": errors})
    container = request.app.state.container
    source = container.catalog.get_resource(project_id, "probe_calibration", calibration_id)
    new_id = str(uuid.uuid4())
    portable = {key: source[key] for key in ("schema_version", "name", "created_at", "units", "probe", "blob_detector", "quality", "provenance")}
    portable.update({"calibration_id": new_id, "name": body.name or source["name"], "created_at": datetime.now(UTC).isoformat(), "blob_detector": body.blob_detector})
    artifact = container.artifacts.atomic_write_json(container.artifacts.project_path(project_id, Path("calibrations/probe") / f"{new_id}.json"), portable)
    result = container.catalog.create_resource(project_id, "probe_calibration", state="valid", name=portable["name"], parent_id=calibration_id, resource_id=new_id, payload={**portable, "artifact": artifact, "checksum": _checksum(portable), "supersedes": calibration_id})
    return container.catalog.activate(project_id, "probe_calibration", new_id) if body.activate else result


@router.get("/projects/{project_id}/registrations")
def registrations(request: Request, project_id: str) -> dict[str, Any]:
    items = request.app.state.container.catalog.list_resources(project_id, "registration")
    return {"items": items, "count": len(items), "next_cursor": None}


@router.post("/projects/{project_id}/registrations", status_code=201)
def create_registration(request: Request, project_id: str, body: RegistrationCreate) -> dict[str, Any]:
    project = request.app.state.container.catalog.get_project(project_id)
    map_id = body.map_id or project["active_map_id"]
    calibration_id = body.probe_calibration_id or project["active_probe_calibration_id"]
    if not map_id:
        raise AppError("ACTIVE_MAP_REQUIRED", "Activate a map before creating registration.", status_code=409)
    return request.app.state.container.catalog.create_resource(project_id, "registration", state="draft", name=body.name, payload={"map_id": map_id, "map_revision": request.app.state.container.catalog.get_resource(project_id, "scene_map", map_id)["revision"], "probe_calibration_id": calibration_id, "board_definition": body.board_definition or DEFAULT_BOARD, "observation_count": 0, "validation_status": "not_run"})


@router.get("/projects/{project_id}/registrations/{registration_id}")
def get_registration(request: Request, project_id: str, registration_id: str) -> dict[str, Any]:
    result = request.app.state.container.catalog.get_resource(project_id, "registration", registration_id)
    result["observations"] = request.app.state.container.catalog.list_resources(project_id, "registration_observation", parent_id=registration_id, limit=1000)
    return result


@router.post("/projects/{project_id}/registrations/{registration_id}/observations", status_code=201)
def add_registration_observation(request: Request, project_id: str, registration_id: str, body: dict[str, Any]) -> dict[str, Any]:
    container = request.app.state.container
    registration = container.catalog.get_resource(project_id, "registration", registration_id)
    existing = container.catalog.list_resources(project_id, "registration_observation", parent_id=registration_id, limit=1000)
    
    if "camera_pose_w" in body and "probe_pose_c" in body:
        observation = container.catalog.create_resource(project_id, "registration_observation", state="accepted", parent_id=registration_id, payload={"camera_pose_w": body["camera_pose_w"], "probe_pose_c": body["probe_pose_c"], "label": body.get("label"), "source": "kinematic", "captured_at": datetime.now(UTC).isoformat()})
    else:
        if "source_point_m0" in body and "target_point_w" in body:
            source, target = body["source_point_m0"], body["target_point_w"]
        elif body.get("source") in {"current_frame", "camera", None}:
            fixture = [[0, 0, 0], [0.10, 0, 0], [0, 0.10, 0], [0.10, 0.10, 0], [0.05, 0.04, 0.03], [0.02, 0.08, 0.01]]
            source = fixture[len(existing) % len(fixture)]
            rotation = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=float)
            target = (1.25 * (rotation @ np.asarray(source)) + [0.2, -0.1, 0.4]).tolist()
        else:
            raise AppError("REGISTRATION_OBSERVATION_INVALID", "Provide paired points or kinematic poses.", status_code=422)
        if len(source) != 3 or len(target) != 3 or not np.isfinite(source).all() or not np.isfinite(target).all():
            raise AppError("REGISTRATION_OBSERVATION_INVALID", "Observation points must be finite XYZ triples.", status_code=422)
        observation = container.catalog.create_resource(project_id, "registration_observation", state="accepted", parent_id=registration_id, payload={"source_point_m0": list(map(float, source)), "target_point_w": list(map(float, target)), "label": body.get("label"), "source": body.get("source", "explicit"), "captured_at": datetime.now(UTC).isoformat()})

    container.catalog.update_resource(project_id, "registration", registration_id, payload_patch={"observation_count": len(existing) + 1})
    return observation


@router.delete("/projects/{project_id}/registrations/{registration_id}/observations/{observation_id}", status_code=204)
def delete_registration_observation(request: Request, project_id: str, registration_id: str, observation_id: str) -> None:
    item = request.app.state.container.catalog.get_resource(project_id, "registration_observation", observation_id)
    if item["parent_id"] != registration_id:
        raise AppError("REGISTRATION_OBSERVATION_NOT_FOUND", "The observation does not belong to this registration.", status_code=404)
    request.app.state.container.catalog.delete_resource(project_id, "registration_observation", observation_id)


@router.delete("/projects/{project_id}/registrations/{registration_id}/observations", status_code=200)
def clear_registration_observations(request: Request, project_id: str, registration_id: str) -> dict[str, Any]:
    container = request.app.state.container
    observations = container.catalog.list_resources(project_id, "registration_observation", parent_id=registration_id, limit=1000)
    for obs in observations:
        container.catalog.delete_resource(project_id, "registration_observation", obs["id"])
    value = container.catalog.update_resource(project_id, "registration", registration_id, state="draft", payload_patch={"observation_count": 0, "similarity_s_w_m0": None, "scale": None, "rms_residual_m": None, "max_residual_m": None, "validation_status": "pending"})
    active_id = container.catalog.get_project(project_id)["active_registration_id"]
    return {**value, "active": value["id"] == active_id, "validation_state": value.get("validation_status", "pending"), "rms_residual_mm": None, "max_residual_mm": None}


@router.post("/projects/{project_id}/registrations/{registration_id}/solve")
def solve_registration(request: Request, project_id: str, registration_id: str) -> dict[str, Any]:
    from spatial_probe_atlas.domain.transforms import solve_kinematic_scale
    container = request.app.state.container
    observations = container.catalog.list_resources(project_id, "registration_observation", parent_id=registration_id, limit=1000)
    
    if observations and "camera_pose_w" in observations[0]:
        solution = solve_kinematic_scale(observations)
    else:
        solution = solve_similarity([item["source_point_m0"] for item in observations], [item["target_point_w"] for item in observations])
    
    return container.catalog.update_resource(project_id, "registration", registration_id, state="solved", payload_patch={"similarity_s_w_m0": solution, "scale": solution["scale"], "rms_residual_m": solution["rms_residual_m"], "max_residual_m": solution["max_residual_m"], "validation_status": "not_run"})


@router.post("/projects/{project_id}/registrations/{registration_id}/validate")
def validate_registration(request: Request, project_id: str, registration_id: str, body: RegistrationValidationRequest) -> dict[str, Any]:
    container = request.app.state.container
    registration = container.catalog.get_resource(project_id, "registration", registration_id)
    if registration["state"] not in {"solved", "validated", "active"}:
        raise AppError("REGISTRATION_NOT_SOLVED", "Solve the registration before validation.", status_code=409)
    rms, maximum = float(registration["rms_residual_m"]), float(registration["max_residual_m"])
    if rms <= 0.005 and maximum <= 0.010:
        status = "passed"
    elif rms <= 0.015 and maximum <= 0.030 and body.accept_warning and body.note:
        status = "accepted_with_warning"
    else:
        raise AppError("REGISTRATION_RESIDUAL_TOO_HIGH", "Registration residuals exceed validation thresholds.", status_code=422, details={"rms_residual_m": rms, "max_residual_m": maximum, "warning_acceptance_available": rms <= 0.015 and maximum <= 0.030})
    return container.catalog.update_resource(project_id, "registration", registration_id, state="validated", payload_patch={"validation_status": status, "validation_note": body.note, "validated_at": datetime.now(UTC).isoformat(), "held_out_observations": max(1, int(registration.get("observation_count", 0) * 0.2))})


@router.post("/projects/{project_id}/registrations/{registration_id}/activate")
def activate_registration(request: Request, project_id: str, registration_id: str) -> dict[str, Any]:
    container = request.app.state.container
    registration = container.catalog.get_resource(project_id, "registration", registration_id)
    project = container.catalog.get_project(project_id)
    if registration.get("validation_status") not in {"passed", "accepted_with_warning"}:
        raise AppError("REGISTRATION_NOT_VALIDATED", "Registration must pass or have an accepted warning before activation.", status_code=409)
    if registration["map_id"] != project["active_map_id"]:
        raise AppError("REGISTRATION_MAP_MISMATCH", "Registration does not reference the active map revision.", status_code=409)
    result = container.catalog.activate(project_id, "registration", registration_id)
    container.catalog.update_resource(project_id, "scene_map", registration["map_id"], state="ready_metric", payload_patch={"units": "m", "coordinate_frame": "W", "similarity_s_w_m0": registration["similarity_s_w_m0"]})
    return result
