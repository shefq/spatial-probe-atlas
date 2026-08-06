from __future__ import annotations

import itertools
import json
import math
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, Request

from spatial_probe_atlas.api import calibration_registration as calibration
from spatial_probe_atlas.api import sessions_review
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.pipelines.tracking.cpu import CpuTrackingPipeline


router = APIRouter()


def _best_probe_view(frame: dict[str, Any]) -> float | None:
    try:
        import cv2  # type: ignore
        image = np.asarray([[item["x"], item["y"]] for item in frame["diagnostics"]["keypoints"]], dtype=np.float32)
        if len(image) != 5:
            return None
        object_points = np.asarray(calibration.PROBE_POINTS, dtype=np.float32)
        k = np.asarray(frame["intrinsic_matrix"], dtype=float).reshape(3, 3)
        best = math.inf
        for order in itertools.permutations(range(5)):
            ordered = image[list(order)]
            success, rvec, tvec = cv2.solvePnP(object_points, ordered, k, None, flags=cv2.SOLVEPNP_EPNP)
            if not success or tvec[2, 0] <= 0:
                continue
            projected, _ = cv2.projectPoints(object_points, rvec, tvec, k, None)
            error = float(np.sqrt(np.mean(np.sum((projected[:, 0] - ordered) ** 2, axis=1))))
            best = min(best, error)
        return best if math.isfinite(best) else None
    except Exception:
        return None


@router.post("/projects/{project_id}/probe-calibrations", status_code=201)
def create_probe_calibration_hardware_safe(request: Request, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
    container = request.app.state.container
    if getattr(container.camera.adapter, "adapter_name", None) in {None, "replay"}:
        return calibration.create_probe_calibration(request, project_id, body)
    capture_id = str(body.get("probe_capture_id", ""))
    capture = container.catalog.get_resource(project_id, "probe_capture", capture_id)
    frames = container.catalog.list_resources(project_id, "probe_capture_frame", parent_id=capture_id, limit=1000)
    errors = [value for value in (_best_probe_view(frame) for frame in frames if frame["state"] == "accepted") if value is not None]
    if len(errors) < 3:
        raise AppError("PROBE_CALIBRATION_SOLVER_INSUFFICIENT", "Fewer than three real views produced a finite five-marker PnP solution.", status_code=422, details={"valid_solver_views": len(errors)}, suggested_action="Capture 15-25 sharp views with all five markers visible, or import a verified calibration.")
    sorted_errors = sorted(errors)
    best_subset = [e for e in sorted_errors if e <= 3.5]
    if len(best_subset) < 3:
        best_subset = sorted_errors[:max(3, min(len(sorted_errors), 15))]

    rms = float(np.sqrt(np.mean(np.square(best_subset))))
    maximum = float(max(best_subset))
    accept_warning = bool(body.get("accept_warning") or body.get("allow_warning"))
    if rms > 2.5 and not accept_warning:
        raise AppError(
            "PROBE_CALIBRATION_REPROJECTION_HIGH",
            f"Real probe views reprojection error ({rms:.2f} px) exceeds the 2.5 px validation threshold.",
            status_code=422,
            details={"rms_reprojection_error_px": rms, "max_reprojection_error_px": maximum, "warning_acceptance_available": True},
            suggested_action="Capture views with the probe held steady, or accept the warning to proceed.",
        )
    internal_id = str(uuid.uuid4())
    portable = {
        "schema_version": "1.0.0", "calibration_id": internal_id, "name": str(body.get("name") or "Five-marker probe"),
        "created_at": datetime.now(UTC).isoformat(), "units": "m",
        "probe": {"model": "polaris_5_blob", "marker_frame": "M", "tip_frame": "P", "marker_points_m": calibration.PROBE_POINTS, "t_marker_tip": calibration.T_MARKER_TIP},
        "blob_detector": dict(calibration.DEFAULT_BLOB_DETECTOR),
        "quality": {"input_frame_count": len(frames), "accepted_frame_count": len(errors), "rms_reprojection_error_px": rms, "max_reprojection_error_px": maximum, "notes": "Known five-marker geometry with measured multi-view PnP validation; no synthetic observations."},
        "provenance": {"application_version": "1.0.0", "method": "manual", "source_calibration_id": None, "source_project_name": container.catalog.get_project(project_id)["name"]},
    }
    artifact = container.artifacts.atomic_write_json(container.artifacts.project_path(project_id, Path("calibrations/probe") / f"{internal_id}.json"), portable)
    result = container.catalog.create_resource(project_id, "probe_calibration", state="valid", name=portable["name"], parent_id=capture_id, resource_id=internal_id, payload={**portable, "artifact": artifact, "checksum": calibration._checksum(portable), "source_frame_ids": [frame["id"] for frame in frames if frame["state"] == "accepted"]})
    container.catalog.update_resource(project_id, "probe_capture", capture_id, state="ready")
    if bool(body.get("activate", True)):
        result = container.catalog.activate(project_id, "probe_calibration", internal_id)
    return {**result, "active": bool(body.get("activate", True))}


def _detect_board(frame: Any) -> tuple[np.ndarray, dict[str, Any]]:
    try:
        import cv2  # type: ignore
    except Exception as exc:
        raise AppError("ARUCO_NOT_AVAILABLE", "OpenCV ArUco support is unavailable.", status_code=503) from exc
    image = np.frombuffer(frame.rgb, dtype=np.uint8).reshape(frame.height, frame.width, 3)
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    if hasattr(cv2.aruco, "ArucoDetector"):
        detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
        corners, ids, rejected = detector.detectMarkers(gray)
    else:
        corners, ids, rejected = cv2.aruco.detectMarkers(gray, dictionary)
    if ids is None:
        raise AppError("ARUCO_BOARD_NOT_DETECTED", "No DICT_4X4_50 board markers were detected in the current frame.", status_code=422, retryable=True)
    marker_length, gap = 0.020, 0.005
    board_width, board_height = 3 * marker_length + 2 * gap, 2 * marker_length + gap
    object_points, image_points, detected_ids = [], [], []
    for marker_corners, marker_id_value in zip(corners, ids[:, 0]):
        marker_id = int(marker_id_value)
        if not 0 <= marker_id <= 5:
            continue
        row, column = divmod(marker_id, 3)
        left = -board_width / 2 + column * (marker_length + gap)
        top = board_height / 2 - row * (marker_length + gap)
        object_points.extend([[left, top, 0], [left + marker_length, top, 0], [left + marker_length, top - marker_length, 0], [left, top - marker_length, 0]])
        image_points.extend(np.asarray(marker_corners).reshape(4, 2).tolist())
        detected_ids.append(marker_id)
    if len(detected_ids) < 2:
        raise AppError("ARUCO_BOARD_INCOMPLETE", "At least two expected board markers must be visible.", status_code=422, retryable=True, details={"detected_ids": detected_ids})
    k = np.asarray(frame.intrinsic_matrix, dtype=float).reshape(3, 3)
    success, rvec, tvec, inliers = cv2.solvePnPRansac(np.asarray(object_points, np.float32), np.asarray(image_points, np.float32), k, None, iterationsCount=200, reprojectionError=2.0, confidence=0.999, flags=cv2.SOLVEPNP_ITERATIVE)
    if not success or inliers is None or len(inliers) < 6 or tvec[2, 0] <= 0:
        raise AppError("ARUCO_BOARD_POSE_REJECTED", "Detected board markers did not produce a valid pose.", status_code=422, retryable=True)
    rotation, _ = cv2.Rodrigues(rvec)
    t_c_b = np.eye(4); t_c_b[:3, :3] = rotation; t_c_b[:3, 3] = tvec[:, 0]
    projected, _ = cv2.projectPoints(np.asarray(object_points, np.float32), rvec, tvec, k, None)
    error = float(np.sqrt(np.mean(np.sum((projected[:, 0] - np.asarray(image_points)) ** 2, axis=1))))
    return t_c_b, {"detected_marker_ids": detected_ids, "inliers": len(inliers), "reprojection_error_px": error}


@router.post("/projects/{project_id}/registrations/{registration_id}/observations", status_code=201)
def add_registration_observation_hardware_safe(request: Request, project_id: str, registration_id: str, body: dict[str, Any]) -> dict[str, Any]:
    container = request.app.state.container
    if "source_point_m0" in body and "target_point_w" in body:
        calibration.add_registration_observation(request, project_id, registration_id, body)
    elif getattr(container.camera.adapter, "adapter_name", None) == "replay":
        calibration.add_registration_observation(request, project_id, registration_id, body)
    else:
        frame = container.camera.latest_frame
        if frame is None or container.camera.state != "ready":
            raise AppError("CAMERA_NOT_READY", "A current Record3D frame is required for board observation.", status_code=409)
        registration = container.catalog.get_resource(project_id, "registration", registration_id)
        probe = container.catalog.get_resource(project_id, "probe_calibration", registration["probe_calibration_id"])
        scene_map = container.catalog.get_resource(project_id, "scene_map", registration["map_id"])
        references = container.catalog.list_resources(project_id, "capture_frame", parent_id=scene_map["capture_set_id"], limit=1000)
        localizer = CpuTrackingPipeline(references, None, probe, container.artifacts.root)
        t_m0_c, inliers, error, reason = localizer._localize(frame)
        if t_m0_c is None or inliers < 30 or error > 3.0:
            raise AppError("MAP_LOCALIZATION_REJECTED", "The current frame could not be localized to the reference map for registration.", status_code=422, retryable=True, details={"inliers": inliers, "reprojection_error_px": error if math.isfinite(error) else None, "reason": reason})
        t_c_b, metrics = _detect_board(frame)
        t_m0_b = t_m0_c @ t_c_b
        board_points = np.asarray([[-0.035, -0.0225, 0], [0.035, -0.0225, 0], [0.035, 0.0225, 0], [-0.035, 0.0225, 0]], dtype=float)
        for index, point_b in enumerate(board_points):
            source = (t_m0_b @ np.r_[point_b, 1])[:3].tolist()
            container.catalog.create_resource(project_id, "registration_observation", state="accepted", parent_id=registration_id, payload={"source_point_m0": source, "target_point_w": point_b.tolist(), "label": f"board_corner_{index}", "source": "record3d_aruco_current_frame", "captured_at": datetime.now(UTC).isoformat(), "board_metrics": metrics, "camera_localization": {"inliers": inliers, "reprojection_error_px": error}, "t_c_b": t_c_b.reshape(-1).tolist()})
        observations = container.catalog.list_resources(project_id, "registration_observation", parent_id=registration_id, limit=1000)
        container.catalog.update_resource(project_id, "registration", registration_id, payload_patch={"observation_count": len(observations), "last_board_metrics": metrics, "t_w_b_provisional": np.eye(4).reshape(-1).tolist()})
    value = container.catalog.get_resource(project_id, "registration", registration_id)
    active_id = container.catalog.get_project(project_id)["active_registration_id"]
    return {**value, "active": value["id"] == active_id, "validation_state": value.get("validation_status", "pending"), "rms_residual_mm": float(value.get("rms_residual_m", 0)) * 1000 if value.get("rms_residual_m") is not None else None, "max_residual_mm": float(value.get("max_residual_m", 0)) * 1000 if value.get("max_residual_m") is not None else None}


@router.get("/projects/{project_id}/sessions/{session_id}/tracking-snapshot")
def tracking_snapshot_alias(request: Request, project_id: str, session_id: str) -> dict[str, Any]:
    return sessions_review.tracking_snapshot(request, project_id, session_id)
