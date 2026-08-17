"""
example_api_client.py
---------------------
Demonstration script showing how to trigger "+ Save point" externally in Spatial Probe Atlas.

How it works:
1. Connects to backend on port 8765.
2. Lets you select a project (or pass via --project-id).
3. Automatically targets the active session open in the browser and keeps it running.
4. Triggers "+ Save Point" every 5s with random labels, colors, and notes.
5. The backend automatically calculates the 3D tip position from live camera tracking
   and instantly broadcasts the committed point to the browser UI ("Recent committed records" + 3D Viewer).

Usage:
    python scripts/example_api_client.py
    python scripts/example_api_client.py --project-id <UUID>
    python scripts/example_api_client.py --interval 5.0

Dependencies:
    pip install requests websockets
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import struct
import sys
import time
import uuid
from typing import Any

try:
    import requests
    import websockets
except ImportError:
    print("Please install required packages: pip install requests websockets")
    sys.exit(1)

TISSUE_COLORS: dict[str, str] = {
    "Fat": "#f5a623",         # Warm Orange / Fat
    "Water": "#0070f3",       # Pure Blue / Hydration
    "Tumor": "#ff007f",       # Vibrant Magenta / Lesion
    "Collagen": "#00df8f",    # Mint Green / Connective
    "Necrosis": "#7928ca",    # Deep Violet / Necrotic
    "Muscle": "#ff3366",      # Crimson / Striated Muscle
    "Blood": "#e00000",       # Blood Red / Vascular
    "Lipid": "#ffff00",       # Bright Yellow / Lipid
    "Stroma": "#50e3c2",      # Turquoise / Stroma
    "Epithelium": "#3388ff",  # Royal Blue / Epithelial
}


class SpatialProbeAtlasClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.base_url = f"http://{host}:{port}/api/v1"
        self.ws_url = f"ws://{host}:{port}/ws/v1"

    # =========================================================================
    # REST API Helpers
    # =========================================================================

    def check_connection(self) -> dict[str, Any]:
        res = requests.get(f"{self.base_url}/system/capabilities", timeout=4)
        res.raise_for_status()
        return res.json()

    def list_projects(self) -> list[dict[str, Any]]:
        res = requests.get(f"{self.base_url}/projects", timeout=5)
        res.raise_for_status()
        data = res.json()
        return data.get("items", data) if isinstance(data, dict) else data

    def list_sessions(self, project_id: str) -> list[dict[str, Any]]:
        res = requests.get(f"{self.base_url}/projects/{project_id}/sessions", timeout=5)
        res.raise_for_status()
        data = res.json()
        return data.get("items", data) if isinstance(data, dict) else data

    def ensure_running_session(self, project_id: str, preferred_session_id: str | None = None) -> tuple[str, str]:
        """
        Finds the target session (or most recent session) and ensures it is in 'running' state.
        Returns (session_id, session_name).
        """
        sessions = self.list_sessions(project_id)

        target_session: dict[str, Any] | None = None

        # 1. Match preferred ID if given
        if preferred_session_id:
            for s in sessions:
                if s["id"].lower() == preferred_session_id.strip().lower():
                    target_session = s
                    break

        # 2. Look for already running session
        if not target_session:
            for s in sessions:
                if s.get("state") == "running":
                    target_session = s
                    break

        # 3. Use the latest session available
        if not target_session and sessions:
            target_session = sessions[0]

        # 4. Create new if none exist
        if not target_session:
            session_name = f"Live Acquisition {time.strftime('%H:%M:%S')}"
            res = requests.post(f"{self.base_url}/projects/{project_id}/sessions", json={"name": session_name}, timeout=5)
            res.raise_for_status()
            target_session = res.json()

        session_id = target_session["id"]
        session_name = target_session.get("name", "Session")
        state = target_session.get("state", "draft")

        # Make sure the session is in running state
        if state != "running":
            action = "start" if state in ("draft", "preflight") else "resume"
            try:
                requests.post(
                    f"{self.base_url}/projects/{project_id}/sessions/{session_id}/lifecycle",
                    json={"action": action},
                    timeout=5,
                )
                print(f"[Session] Activated '{session_name}' ({action}) -> state: running")
            except Exception as exc:
                print(f"[Session] Note: Could not transition session '{session_name}': {exc}")

        return session_id, session_name

    def trigger_save_point(
        self,
        project_id: str,
        session_id: str,
        label: str,
        color: str,
        value: float | None = None,
        manual_position: list[float] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """
        Triggers "+ Save Point".
        The backend automatically estimates the 3D tip position from live camera tracking
        and broadcasts the committed point to all live UI viewers.
        """
        payload = {
            "command_id": str(uuid.uuid4()),
            "position_w_m": manual_position,
            "label": label,
            "color": color,
            "value": value,
            "note": note,
            "save_image": True,
            "low_quality_override_reason": "external_api_trigger",
        }
        res = requests.post(
            f"{self.base_url}/projects/{project_id}/sessions/{session_id}/painted-points",
            json=payload,
            timeout=5,
        )
        if res.status_code == 409:
            # Re-ensure running session and retry once
            session_id, _ = self.ensure_running_session(project_id, session_id)
            res = requests.post(
                f"{self.base_url}/projects/{project_id}/sessions/{session_id}/painted-points",
                json=payload,
                timeout=5,
            )
        res.raise_for_status()
        return res.json()


# =============================================================================
# Periodic "+ Save Point" Trigger Loop
# =============================================================================

async def run_point_emitter(
    client: SpatialProbeAtlasClient,
    project_id: str,
    session_id: str,
    session_name: str,
    interval_sec: float = 8.0,
    manual_coords: bool = False,
) -> None:
    print(f"\n" + "=" * 75)
    print(f"🚀 Triggering '+ Save Point' every {interval_sec}s")
    print(f"   Project ID : {project_id}")
    print(f"   Session    : '{session_name}' ({session_id})")
    print(f"   Position   : {'Manual Injection' if manual_coords else 'Auto-Estimated by App from Live Tracking'}")
    print(f"   Broadcast  : Live to Browser UI ('Recent committed records' + 3D Map)")
    print(f"   Press Ctrl+C in terminal to stop.")
    print("=" * 75 + "\n")

    point_num = 0
    while True:
        point_num += 1

        label = random.choice(list(TISSUE_COLORS.keys()))
        color = TISSUE_COLORS[label]
        pct = round(random.uniform(5.0, 95.0), 1)
        val = pct
        note = f"{label} concentration: {pct}%"

        manual_pos = None
        if manual_coords:
            manual_pos = [
                round(random.uniform(-0.10, 0.10), 4),
                round(random.uniform(-0.10, 0.10), 4),
                round(random.uniform(0.25, 0.50), 4),
            ]

        try:
            result = client.trigger_save_point(
                project_id=project_id,
                session_id=session_id,
                label=label,
                color=color,
                value=val,
                manual_position=manual_pos,
                note=note,
            )
            timestamp = time.strftime("%H:%M:%S")

            raw_pos = result.get("position_w_m") or result.get("position") or []
            quality = result.get("quality", "good")

            if raw_pos and len(raw_pos) == 3:
                pos_str = f"[{raw_pos[0]:+.3f}, {raw_pos[1]:+.3f}, {raw_pos[2]:+.3f}] m"
            else:
                pos_str = "[Frame captured / Awaiting Probe Visibility]"

            print(
                f"[{timestamp}] Trigger #{point_num:03d} -> "
                f"Tissue: {label:<10} | Value: {pct:5.1f}% | Color: {color} | "
                f"Pos: {pos_str:<32} | Quality: {quality}"
            )
        except requests.HTTPError as exc:
            print(f"[{time.strftime('%H:%M:%S')}] Trigger #{point_num} HTTP Error: {exc}")
            session_id, session_name = client.ensure_running_session(project_id, session_id)
        except Exception as exc:
            print(f"[{time.strftime('%H:%M:%S')}] Failed to trigger point #{point_num}: {exc}")

        await asyncio.sleep(interval_sec)


# =============================================================================
# Interactive Project Selection
# =============================================================================

def select_project_and_session(
    client: SpatialProbeAtlasClient,
    cli_project_id: str | None,
    cli_session_id: str | None,
) -> tuple[str, str, str]:
    projects = client.list_projects()
    if not projects:
        print("[Error] No projects exist on the server. Please create one in the UI first.")
        sys.exit(1)

    project_id: str | None = None
    project_name = ""

    if cli_project_id:
        for p in projects:
            if p["id"].lower() == cli_project_id.strip().lower():
                project_id = p["id"]
                project_name = p["name"]
                break
        if not project_id:
            project_id = cli_project_id.strip()
            project_name = "Selected Project"
    else:
        print("\n--- Available Projects ---")
        for idx, p in enumerate(projects, 1):
            print(f"  [{idx:2d}] {p['name']:<30} (ID: {p['id']})")
        print("--------------------------")

        while True:
            try:
                choice = input(f"\nEnter Project ID or number [1-{len(projects)}] (default: 1): ").strip()
                if not choice:
                    selected = projects[0]
                    project_id = selected["id"]
                    project_name = selected["name"]
                    break

                if choice.isdigit():
                    num = int(choice)
                    if 1 <= num <= len(projects):
                        selected = projects[num - 1]
                        project_id = selected["id"]
                        project_name = selected["name"]
                        break

                for p in projects:
                    if p["id"].lower() == choice.lower():
                        project_id = p["id"]
                        project_name = p["name"]
                        break
                if project_id:
                    break

                if len(choice) >= 8:
                    project_id = choice
                    project_name = "Custom Project"
                    break

                print("Invalid input. Please enter a valid number or project ID.")
            except (KeyboardInterrupt, EOFError):
                print("\nExiting.")
                sys.exit(0)

    print(f"[OK] Project: '{project_name}' ({project_id})")
    session_id, session_name = client.ensure_running_session(project_id, cli_session_id)
    return project_id, session_id, session_name


async def async_main(args: argparse.Namespace) -> None:
    client = SpatialProbeAtlasClient(host=args.host, port=args.port)

    # 1. Verify connection
    try:
        caps = client.check_connection()
        print(f"Connected to Spatial Probe Atlas (v{caps.get('app_version', '1.0.0')}) on http://{args.host}:{args.port}")
    except Exception as exc:
        print(f"[Error] Could not connect to backend at http://{args.host}:{args.port}: {exc}")
        print("Make sure the backend is running (run.bat).")
        sys.exit(1)

    # 2. Select Project and active Session
    project_id, session_id, session_name = select_project_and_session(client, args.project_id, args.session_id)

    # 3. Start periodic point trigger loop
    await run_point_emitter(
        client,
        project_id,
        session_id,
        session_name,
        interval_sec=args.interval,
        manual_coords=args.manual_coords,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Spatial Probe Atlas External '+ Save Point' Trigger Client")
    parser.add_argument("--host", default="127.0.0.1", help="Server hostname (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Server port (default: 8765)")
    parser.add_argument("--project-id", default=None, help="Target Project ID (optional; prompted if not specified)")
    parser.add_argument("--session-id", default=None, help="Target Session ID (optional; defaults to active session)")
    parser.add_argument("--interval", type=float, default=5.0, help="Interval in seconds between point triggers (default: 5.0)")
    parser.add_argument("--manual-coords", action="store_true", help="Send manual coordinates instead of auto-estimating position from live camera tracking")
    args = parser.parse_args()

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\n[Stopped] Client exited.")


if __name__ == "__main__":
    main()
