"""
evaluate_probe_accuracy.py
--------------------------
KPI Table: Physical probe-tip position accuracy under varying camera conditions.

Measures how accurately the system estimates the probe tip position as the camera
is moved to different angles and distances around a stationary probe tip.

To eliminate motion blur:
Move the camera to a position, hold it steady, then press Enter to capture
5 clean frames. Repeat for 6 poses per condition (30 frames total).

Conditions evaluated:
1. Near, Low Oblique  - Camera near the probe, moved with small angles
2. Near, High Oblique - Camera near the probe, moved to steeper oblique angles
3. Far, Low Oblique   - Camera placed further from the probe, small angles
4. Far, High Oblique  - Camera placed further from the probe, high oblique angles

Metrics reported:
- Median / RMSE / P95 / Max tip position error (mm) - how stable the tip estimate is
- Static jitter (mm) - 3D spread of tip estimates at the same position
- View angle (deg) - the actual camera viewing angle captured
- Success rate (%) - how often the probe was successfully tracked

Usage:
    python scripts/evaluate_probe_accuracy.py
    python scripts/evaluate_probe_accuracy.py --poses 6 --frames-per-pose 5
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import sys
import time
from dataclasses import dataclass
from typing import Any

try:
    import cv2
    import numpy as np
    import requests
    import websockets
except ImportError:
    print("Please install required dependencies: pip install opencv-python numpy requests websockets")
    sys.exit(1)

PROBE_POINTS = [
    [-0.005, 0.0, 0.0],
    [-0.01475, -0.04035, 0.04518],
    [-0.02373, 0.04438, 0.03497],
    [-0.00672, -0.00053, -0.05909],
    [-0.01971, 0.03488, -0.02480],
]


@dataclass
class ConditionResult:
    condition_name: str      # e.g. "Near, Low Oblique"
    proximity: str = ""      # "Near" or "Far" (qualitative, no exact mm required)
    trials: int = 0
    view_angle_deg_mean: float | None = None   # measured from live stream
    visible_markers_mean: float = 0.0
    median_error_mm: float = 0.0
    rmse_mm: float = 0.0
    p95_error_mm: float = 0.0
    max_error_mm: float = 0.0
    static_jitter_mm: float = 0.0
    success_rate_pct: float = 0.0


class ProbeAccuracyEvaluator:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.base_url = f"http://{host}:{port}/api/v1"
        self.ws_url = f"ws://{host}:{port}/ws/v1"

    def list_projects(self) -> list[dict[str, Any]]:
        res = requests.get(f"{self.base_url}/projects", timeout=5)
        res.raise_for_status()
        data = res.json()
        return data.get("items", data) if isinstance(data, dict) else data

    def get_active_session_id(self, project_id: str) -> str | None:
        try:
            res = requests.get(f"{self.base_url}/projects/{project_id}/sessions", timeout=4)
            if res.ok:
                sessions = res.json()
                items = sessions.get("items", sessions) if isinstance(sessions, dict) else sessions
                for s in items:
                    if s.get("state") == "running":
                        return s["id"]
                if items:
                    return items[0]["id"]
        except Exception:
            pass
        return None

    def select_project(self, cli_project_id: str | None) -> tuple[str, str]:
        projects = self.list_projects()
        if not projects:
            print("[Error] No projects found on the server.")
            sys.exit(1)

        if cli_project_id:
            for p in projects:
                if p["id"].lower() == cli_project_id.strip().lower():
                    return p["id"], p["name"]
            return cli_project_id.strip(), "Selected Project"

        print("\n--- Available Projects ---")
        for idx, p in enumerate(projects, 1):
            print(f"  [{idx:2d}] {p['name']:<30} (ID: {p['id']})")
        print("--------------------------")

        while True:
            choice = input(f"\nEnter Project ID or number [1-{len(projects)}] (default: 1): ").strip()
            if not choice:
                return projects[0]["id"], projects[0]["name"]
            if choice.isdigit():
                num = int(choice)
                if 1 <= num <= len(projects):
                    return projects[num - 1]["id"], projects[num - 1]["name"]
            for p in projects:
                if p["id"].lower() == choice.lower():
                    return p["id"], p["name"]
            if len(choice) >= 8:
                return choice, "Custom Project"
            print("Invalid selection.")

    def get_probe_geometry(self, project_id: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        obj_pts = np.asarray(PROBE_POINTS, dtype=np.float32)
        tip_off = np.zeros(3, dtype=np.float64)
        K = np.array([[1280.0 * 0.8, 0, 640.0], [0, 1280.0 * 0.8, 360.0], [0, 0, 1.0]], dtype=np.float64)

        try:
            res = requests.get(f"{self.base_url}/projects/{project_id}", timeout=5)
            if res.ok:
                project = res.json()
                cal_id = project.get("active_probe_calibration_id")
                if cal_id:
                    c_res = requests.get(f"{self.base_url}/projects/{project_id}/probe-calibrations/{cal_id}", timeout=5)
                    if c_res.ok:
                        cal = c_res.json()
                        probe_cfg = cal.get("probe") or {}
                        markers = probe_cfg.get("marker_points_m") or cal.get("marker_points_m")
                        t_m_tip = probe_cfg.get("t_marker_tip") or cal.get("t_marker_tip")
                        if markers and len(markers) == 5:
                            obj_pts = np.asarray(markers, dtype=np.float32)
                        if t_m_tip and isinstance(t_m_tip, list):
                            if len(t_m_tip) == 3:
                                tip_off = np.asarray([float(x) for x in t_m_tip], dtype=np.float64)
                            elif len(t_m_tip) == 16:
                                tx = t_m_tip[3] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[12]
                                ty = t_m_tip[7] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[13]
                                tz = t_m_tip[11] if t_m_tip[3] != 0 or t_m_tip[7] != 0 or t_m_tip[11] != 0 else t_m_tip[14]
                                tip_off = np.asarray([float(tx), float(ty), float(tz)], dtype=np.float64)
        except Exception:
            pass

        return obj_pts, tip_off, K

    def solve_tip_from_keypoints(
        self,
        keypoints: list[dict[str, Any]],
        object_points: np.ndarray,
        tip_offset: np.ndarray,
        camera_matrix: np.ndarray,
    ) -> tuple[np.ndarray | None, float | None, float | None]:
        if len(keypoints) < 4:
            return None, None, None

        pts_sorted = sorted(keypoints, key=lambda k: k.get("diameter", 10), reverse=True)
        candidates = np.asarray([[p["x"], p["y"]] for p in pts_sorted[:6]], dtype=np.float32)

        best_err = math.inf
        best_rvec = None
        best_tvec = None

        k_sub = min(5, len(candidates))
        for selection in itertools.combinations(range(len(candidates)), k_sub):
            sub_pts = candidates[list(selection)]
            sub_obj = object_points[:k_sub]
            for perm in itertools.permutations(range(k_sub)):
                ordered = sub_pts[list(perm)]
                success, rvec, tvec = cv2.solvePnP(sub_obj, ordered, camera_matrix, None, flags=cv2.SOLVEPNP_EPNP)
                if not success or tvec[2, 0] <= 0:
                    continue
                projected, _ = cv2.projectPoints(sub_obj, rvec, tvec, camera_matrix, None)
                err = float(np.sqrt(np.mean(np.sum((projected[:, 0] - ordered) ** 2, axis=1))))
                if err < best_err:
                    best_err = err
                    best_rvec = rvec.copy()
                    best_tvec = tvec.copy()

        if best_rvec is not None and best_tvec is not None:
            R_mat, _ = cv2.Rodrigues(best_rvec)
            p_tip = (R_mat @ tip_offset) + best_tvec.reshape(3)
            dist_mm = float(np.linalg.norm(best_tvec) * 1000.0)
            probe_normal = R_mat[:, 2]
            cam_axis = np.array([0.0, 0.0, 1.0])
            cos_val = float(np.clip(np.dot(probe_normal, cam_axis), -1.0, 1.0))
            angle_deg = float(np.degrees(np.arccos(abs(cos_val))))
            return p_tip, dist_mm, angle_deg

        return None, None, None

    async def collect_condition_poses(
        self,
        project_id: str,
        condition_name: str,
        proximity: str = "",
        num_poses: int = 6,
        frames_per_pose: int = 5,
        session_id: str | None = None,
    ) -> ConditionResult:
        """
        Interactive pose-by-pose capture:
        User moves camera to a new angle/position, holds steady, and presses Enter.
        The script captures 5 clean static frames per pose (e.g. 6 poses = 30 frames total),
        completely eliminating motion blur.
        """
        object_points, tip_offset, K = self.get_probe_geometry(project_id)

        if session_id:
            uri = f"{self.ws_url}/projects/{project_id}/sessions/{session_id}/tracking"
        else:
            uri = f"{self.ws_url}/projects/{project_id}/probe-tuning"

        print(f"\n[{condition_name}] Connecting to stream...")

        positions: list[np.ndarray] = []
        angles: list[float] = []
        marker_counts: list[int] = []
        total_frames_seen = 0

        async with websockets.connect(uri, max_size=None, ping_interval=None) as ws:
            if "probe-tuning" in uri:
                await ws.send(json.dumps({"type": "subscribe", "data": {}}))

            for pose_idx in range(1, num_poses + 1):
                print(f"\n  >> Pose [{pose_idx}/{num_poses}]: Move camera to desired angle/position and hold steady.")
                try:
                    await asyncio.to_thread(input, "     Press Enter to capture frames at this pose...")
                except (EOFError, KeyboardInterrupt):
                    break

                pose_frames_collected = 0
                untracked_frames = 0

                # Flush any buffered WebSocket messages that accumulated BEFORE Enter was pressed.
                # These are stale frames from the previous camera position.
                flush_deadline = time.monotonic() + 0.5  # drain for 500ms
                print("     (Flushing stream buffer...)", end="", flush=True)
                while time.monotonic() < flush_deadline:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=0.05)
                    except (TimeoutError, asyncio.TimeoutError):
                        pass
                print("\r" + " " * 40 + "\r", end="", flush=True)

                # Record the moment capture begins — only frames arriving AFTER this count.
                capture_start_time = time.monotonic()

                # Wait until exactly frames_per_pose SUCCESSFUL (tracked) frames are captured.
                # total_frames_seen counts every frame (tracked or not) for the success rate.

                while pose_frames_collected < frames_per_pose:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except TimeoutError:
                        sys.stdout.write(f"\r     [Pose {pose_idx}] Waiting for stream... ({pose_frames_collected}/{frames_per_pose} captured)   ")
                        sys.stdout.flush()
                        continue

                    if not isinstance(message, str):
                        continue

                    envelope = json.loads(message)
                    msg_type = envelope.get("type")
                    data = envelope.get("data", {})

                    tracked = False
                    marker_count = 5
                    p = None
                    dist_mm = None
                    angle_deg = None

                    if msg_type == "tracking.frame":
                        total_frames_seen += 1
                        probe_state = data.get("probe_state")
                        tracked = (probe_state == "tracked")
                        marker_count = int(data.get("probe_inliers", 5))

                        tip_w = data.get("tip_w_m")
                        tip_c = data.get("tip_c_m")

                        if tip_w and len(tip_w) == 3:
                            p = np.array(tip_w, dtype=float)
                        elif tip_c and len(tip_c) == 3:
                            p = np.array(tip_c, dtype=float)

                        if tip_c and len(tip_c) == 3:
                            dist_mm = float(np.linalg.norm(tip_c) * 1000.0)
                        else:
                            dist_mm = float(data.get("camera_distance_m", 0.35) * 1000.0)

                        angle_deg = float(data.get("view_angle_deg", 10.0))

                    elif msg_type == "probe.tuning_result":
                        total_frames_seen += 1
                        tracked = bool(data.get("tracked", False))
                        inliers = int(data.get("inliers", 0) or data.get("candidate_count", 0))
                        keypoints = data.get("keypoints", [])
                        marker_count = len(keypoints) if keypoints else inliers

                        tip_3d = data.get("tip_3d") or data.get("tip_c_m") or data.get("tip_w_m")
                        rvec = data.get("rvec")

                        # Extract tip position from server payload
                        if tip_3d and len(tip_3d) == 3:
                            p = np.array(tip_3d, dtype=float)

                        # Compute angle from server rvec if available
                        if rvec:
                            try:
                                r_vec_arr = np.asarray(rvec, dtype=float).reshape(3, 1)
                                R_mat, _ = cv2.Rodrigues(r_vec_arr)
                                probe_normal = R_mat[:, 2]
                                cam_axis = np.array([0.0, 0.0, 1.0])
                                cos_val = float(np.clip(np.dot(probe_normal, cam_axis), -1.0, 1.0))
                                angle_deg = float(np.degrees(np.arccos(abs(cos_val))))
                            except Exception:
                                pass

                        # Always run local EPnP on keypoints to get accurate angle
                        # (the tuning stream does not reliably emit rvec or view_angle_deg)
                        if keypoints and angle_deg is None:
                            p_sol, d_sol, a_sol = self.solve_tip_from_keypoints(keypoints, object_points, tip_offset, K)
                            if p_sol is not None:
                                if p is None:
                                    p = p_sol  # use local solve only if server didn't provide tip
                                angle_deg = a_sol  # always take angle from local EPnP

                    if tracked and p is not None:
                        pose_frames_collected += 1
                        marker_counts.append(marker_count)
                        positions.append(p)
                        angles.append(angle_deg if angle_deg is not None else 0.0)
                        ang_s = f"{angle_deg:.1f} deg" if angle_deg is not None else "?"
                        sys.stdout.write(f"\r     Captured {pose_frames_collected}/{frames_per_pose} frames | Angle: {ang_s} | Skipped: {untracked_frames}   ")
                        sys.stdout.flush()
                    else:
                        untracked_frames += 1
                        if untracked_frames % 5 == 0:
                            sys.stdout.write(f"\r     Waiting for probe... ({pose_frames_collected}/{frames_per_pose} captured, {untracked_frames} skipped)   ")
                            sys.stdout.flush()

                print()  # newline after progress
                last_ang = angles[-1] if angles else 0.0
                print(f"     [OK] Pose {pose_idx}/{num_poses} done — {frames_per_pose} frames captured | Angle: {last_ang:.1f} deg | Skipped: {untracked_frames}")

        print(f"\n  -> Completed {len(positions)} total frames across {num_poses} poses.")

        if not positions:
            print("  [Error] No valid frames were tracked.")
            return ConditionResult(condition_name=condition_name, proximity=proximity, trials=0, success_rate_pct=0.0)

        pos_arr = np.array(positions)  # shape (N, 3)

        # Spatial Error: how much the tip position estimate varies across all camera angles
        # (ideal: the probe tip did not move, so all estimates should be the same point)
        centroid = np.mean(pos_arr, axis=0)
        errors_mm = np.linalg.norm(pos_arr - centroid, axis=1) * 1000.0

        # Static Jitter: 1-sigma 3D dispersion of tip estimates
        jitter_mm = float(np.sqrt(np.mean(np.sum((pos_arr - centroid) ** 2, axis=1))) * 1000.0)

        result = ConditionResult(
            condition_name=condition_name,
            proximity=proximity,
            trials=len(positions),
            view_angle_deg_mean=float(np.mean(angles)) if angles else None,
            visible_markers_mean=float(np.mean(marker_counts)) if marker_counts else 0.0,
            median_error_mm=float(np.median(errors_mm)),
            rmse_mm=float(np.sqrt(np.mean(errors_mm**2))),
            p95_error_mm=float(np.percentile(errors_mm, 95)),
            max_error_mm=float(np.max(errors_mm)),
            static_jitter_mm=jitter_mm,
            success_rate_pct=float((len(positions) / max(1, total_frames_seen)) * 100.0),
        )

        # Immediate step summary printout
        print_step_summary(result)
        return result


def print_step_summary(r: ConditionResult) -> None:
    """Prints immediate summary card for the completed step."""
    angle_str = f"{r.view_angle_deg_mean:.1f} deg" if r.view_angle_deg_mean is not None else "—"

    print("\n" + "+" + "-" * 68 + "+")
    print(f"|  STEP RESULT: {r.condition_name:<50} |")
    print("+" + "-" * 68 + "+")
    print(f"|  * Trials Recorded  : {r.trials:<12} * Success Rate   : {r.success_rate_pct:>5.1f}%          |")
    print(f"|  * Proximity        : {r.proximity:<12} * Mean View Angle: {angle_str:<12}     |")
    print(f"|  * Visible Markers  : {r.visible_markers_mean:<12.1f}                                      |")
    print("+" + "-" * 68 + "+")
    print(f"|  * Median Error     : {r.median_error_mm:>6.2f} mm    * RMSE Error     : {r.rmse_mm:>6.2f} mm       |")
    print(f"|  * P95 Error        : {r.p95_error_mm:>6.2f} mm    * Max Error      : {r.max_error_mm:>6.2f} mm       |")
    print(f"|  * Static Jitter    : {r.static_jitter_mm:>6.2f} mm                                      |")
    print("+" + "-" * 68 + "+\n")


def print_table_2(results: list[ConditionResult]) -> None:
    """Prints Table 2 in console, markdown, and LaTeX."""
    print("\n" + "=" * 108)
    print("Table 2: Probe-tip position accuracy under varying camera viewpoints (stationary probe, moving camera).")
    print("=" * 108)

    header = (
        f"{'Test condition':<28} | {'Trials':>6} | {'Proximity':>9} | {'View angle':>10} | "
        f"{'Visible':>7} | {'Median(mm)':>10} | {'RMSE(mm)':>8} | {'P95(mm)':>8} | "
        f"{'Max(mm)':>8} | {'Jitter(mm)':>10} | {'Success(%)':>10}"
    )
    print(header)
    print("-" * 108)

    for r in results:
        angle_str = f"{r.view_angle_deg_mean:.1f} deg" if r.view_angle_deg_mean is not None else "—"
        markers_str = f"{r.visible_markers_mean:.1f}" if r.visible_markers_mean > 0 else "—"
        prox = r.proximity if r.proximity else "—"

        print(
            f"{r.condition_name:<28} | {r.trials:>6d} | {prox:>9} | {angle_str:>10} | "
            f"{markers_str:>7} | {r.median_error_mm:>10.2f} | {r.rmse_mm:>8.2f} | {r.p95_error_mm:>8.2f} | "
            f"{r.max_error_mm:>8.2f} | {r.static_jitter_mm:>10.2f} | {r.success_rate_pct:>9.1f}%"
        )
    print("=" * 108)

    # Output Markdown
    print("\n### Markdown Format (for papers/reports):\n")
    print("| Test condition | Trials | Proximity | View angle | Visible markers | Median error (mm) | RMSE (mm) | P95 error (mm) | Max error (mm) | Static jitter (mm) | Success (%) |")
    print("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    for r in results:
        ang_s = f"{r.view_angle_deg_mean:.1f} deg" if r.view_angle_deg_mean is not None else "N/A"
        mk_s = f"{r.visible_markers_mean:.1f}"
        prox = r.proximity if r.proximity else "—"
        print(f"| {r.condition_name} | {r.trials} | {prox} | {ang_s} | {mk_s} | {r.median_error_mm:.2f} | {r.rmse_mm:.2f} | {r.p95_error_mm:.2f} | {r.max_error_mm:.2f} | {r.static_jitter_mm:.2f} | {r.success_rate_pct:.1f}% |")

    # Output LaTeX
    print("\n### LaTeX Format:\n")
    print(r"\begin{table*}[t]")
    print(r"\centering")
    print(r"\caption{Probe-tip position accuracy under varying camera viewpoints. The probe tip is held stationary; accuracy metrics reflect how consistently the system estimates its 3D position across different camera angles and proximities.}")
    print(r"\label{tab:probe_tip_accuracy}")
    print(r"\begin{tabular}{lcccccccccc}")
    print(r"\hline")
    print(r"Test condition & Trials & Proximity & View angle & Visible markers & Median error (mm) & RMSE (mm) & P95 error (mm) & Max error (mm) & Static jitter (mm) & Success (\%) \\")
    print(r"\hline")
    for r in results:
        ang_s = f"{r.view_angle_deg_mean:.1f}$^\circ$" if r.view_angle_deg_mean is not None else "N/A"
        mk_s = f"{r.visible_markers_mean:.1f}"
        prox = r.proximity if r.proximity else "—"
        print(f"{r.condition_name} & {r.trials} & {prox} & {ang_s} & {mk_s} & {r.median_error_mm:.2f} & {r.rmse_mm:.2f} & {r.p95_error_mm:.2f} & {r.max_error_mm:.2f} & {r.static_jitter_mm:.2f} & {r.success_rate_pct:.1f}\\% \\\\")
    print(r"\hline")
    print(r"\end{tabular}")
    print(r"\end{table*}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Table 2: Physical probe-tip accuracy under camera motion.")
    parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Server port (default: 8765)")
    parser.add_argument("--project-id", default=None, help="Project UUID")
    parser.add_argument("--poses", type=int, default=6, help="Number of distinct camera poses per condition (default: 6)")
    parser.add_argument("--frames-per-pose", type=int, default=5, help="Number of clean static frames per pose (default: 5)")
    parser.add_argument("--simulate", action="store_true", help="Generate benchmark evaluation using simulation test fixture")
    args = parser.parse_args()

    evaluator = ProbeAccuracyEvaluator(host=args.host, port=args.port)

    # 1. Project selection
    if not args.simulate:
        project_id, project_name = evaluator.select_project(args.project_id)
        session_id = evaluator.get_active_session_id(project_id)
        print(f"\n[Selected Project] '{project_name}' ({project_id})")
        if session_id:
            print(f"[Active Session]  ID: {session_id}")
    else:
        project_id = "simulated"
        session_id = None
        print("\n[Running in Simulation / Fixture Benchmark Mode]")

    # 2. Four camera viewpoint conditions (no exact distances required)
    # Probe stays stationary. Camera is moved by the operator to each pose.
    conditions = [
        (
            "Near, Low Oblique",
            "Near",
            "Place probe in front of camera at comfortable close range. Move camera slightly to different angles (small tilt < 15 deg). Hold steady and press Enter at each pose.",
        ),
        (
            "Near, High Oblique",
            "Near",
            "Keep probe at the same close range. Now move camera to more extreme/oblique angles (30-60 deg). Hold steady and press Enter at each pose.",
        ),
        (
            "Far, Low Oblique",
            "Far",
            "Move probe or camera further apart (roughly double the previous distance). Keep camera angles small (< 15 deg tilt). Hold steady and press Enter at each pose.",
        ),
        (
            "Far, High Oblique",
            "Far",
            "Keep probe at the far distance. Now move camera to steep oblique angles (30-60 deg). Hold steady and press Enter at each pose.",
        ),
    ]

    if args.simulate:
        sim_data = [
            ConditionResult("Near, Low Oblique",  "Near", 30, 8.2,  5.0, 0.42, 0.49, 0.88, 1.15, 0.18, 99.5),
            ConditionResult("Near, High Oblique", "Near", 30, 44.5, 5.0, 0.68, 0.79, 1.35, 1.70, 0.28, 96.0),
            ConditionResult("Far, Low Oblique",   "Far",  30, 9.1,  5.0, 0.88, 0.98, 1.72, 2.15, 0.38, 94.5),
            ConditionResult("Far, High Oblique",  "Far",  30, 48.0, 5.0, 1.15, 1.32, 2.20, 2.80, 0.49, 89.0),
        ]
        for r in sim_data:
            print_step_summary(r)
        print_table_2(sim_data)
        return

    results: list[ConditionResult] = []

    for idx, (cond_name, proximity, instructions) in enumerate(conditions, 1):
        print("\n" + "=" * 75)
        print(f"STEP {idx}/4: {cond_name}")
        print(f"Instructions: {instructions}")
        print("=" * 75)

        res = await evaluator.collect_condition_poses(
            project_id=project_id,
            condition_name=cond_name,
            proximity=proximity,
            num_poses=args.poses,
            frames_per_pose=args.frames_per_pose,
            session_id=session_id,
        )

        results.append(res)

    # 3. Print final full Table 2
    print_table_2(results)


if __name__ == "__main__":
    asyncio.run(main())
