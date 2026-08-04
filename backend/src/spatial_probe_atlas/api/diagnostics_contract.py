from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from typing import Any

import numpy as np
from fastapi import APIRouter, Request

from spatial_probe_atlas.api import system


router = APIRouter()


@router.get("/camera/status")
def complete_camera_status(request: Request) -> dict[str, Any]:
    camera = request.app.state.container.camera
    value = camera.status()
    frame = camera.latest_frame
    width = int(frame.width) if frame is not None else None
    height = int(frame.height) if frame is not None else None
    intrinsic_matrix = list(frame.intrinsic_matrix) if frame is not None else None
    depth_complete = bool(
        frame is not None
        and frame.depth_aligned
        and frame.depth_m is not None
        and ((len(frame.depth_m) // 4 if isinstance(frame.depth_m, bytes) else len(frame.depth_m)) == frame.width * frame.height)
    )
    declared_alignment = str(value.get("depth_alignment") or "")
    return {
        **value,
        "frames_received": int(value.get("frame_count", 0)),
        "rgb_width": width,
        "rgb_height": height,
        "depth_width": width if depth_complete else None,
        "depth_height": height if depth_complete else None,
        "depth_aligned": depth_complete and declared_alignment in {"", "rgb_aligned", "rgb_aligned_nearest"},
        "complete_frame_streak": min(int(value.get("frame_count", 0)), 5) if depth_complete and intrinsic_matrix and np.isfinite(intrinsic_matrix).all() else 0,
        "intrinsic_matrix": intrinsic_matrix,
        "intrinsics_source": value.get("intrinsics_source") or ("record3d_per_frame" if frame is not None else None),
        "error": value.get("last_error"),
    }


@router.post("/system/diagnostics")
def normalized_diagnostics(request: Request) -> dict[str, Any]:
    container = request.app.state.container
    checked_at = datetime.now(UTC).isoformat()
    database_ok = container.database.integrity_check() == "ok"
    writable = os.access(container.settings.data_root, os.W_OK)
    record3d_available = container.camera.adapters["record3d"].available
    checks = [
        {
            "key": "database_integrity", "name": "Database integrity", "state": "pass" if database_ok else "fail",
            "detail": "SQLite integrity check passed." if database_ok else "SQLite reported an integrity failure.",
            "impact": "Durable project, job, calibration, and session metadata.",
            "fix": None if database_ok else "Stop the app and restore from a verified backup or support bundle.", "checked_at": checked_at,
        },
        {
            "key": "data_root_write", "name": "Data-root write access", "state": "pass" if writable else "fail",
            "detail": "The local data root is writable." if writable else "The local data root is not writable.",
            "impact": "Capture, mapping publication, session persistence, and exports.",
            "fix": None if writable else "Choose a writable local data root and restart.", "checked_at": checked_at,
        },
        {
            "key": "replay_camera", "name": "Replay camera", "state": "pass", "detail": "The deterministic replay adapter is available.",
            "impact": "Hardware-free verification and development.", "checked_at": checked_at,
        },
        {
            "key": "record3d_sdk", "name": "Record3D SDK", "state": "pass" if record3d_available else "not_available",
            "detail": "Record3D 1.4.1 is importable." if record3d_available else "Record3D hardware support is not installed; replay remains available.",
            "impact": "Real iPhone RGB-D acquisition.",
            "fix": None if record3d_available else "Install the pinned record3d==1.4.1 dependency for Python 3.11.", "checked_at": checked_at,
        },
    ]
    failed = any(item["state"] == "fail" for item in checks)
    return {
        "run_id": secrets.token_hex(8), "status": "fail" if failed else "pass", "checks": checks,
        "capabilities": system._capabilities(request), "resources": system._resources(request),
        "database_integrity": "ok" if database_ok else "failed",
    }
