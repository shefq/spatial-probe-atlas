from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import numpy as np
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.ports.camera import NormalizedCameraFrame

from .record3d import Record3DAdapter
from .replay import ReplayCameraAdapter


class CameraService:
    def __init__(self) -> None:
        self.adapters = {"replay": ReplayCameraAdapter(), "record3d": Record3DAdapter()}
        self.adapter: ReplayCameraAdapter | Record3DAdapter | None = None
        self.connection_id: str | None = None
        self.project_id: str | None = None
        self.owner: str | None = None
        self.state: str = "disconnected"
        self.latest_frame: NormalizedCameraFrame | None = None
        self._task: asyncio.Task[None] | None = None
        self._condition = asyncio.Condition()
        self._connected_at: float | None = None
        self._verified_frames: int = 0

    def devices(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for name, adapter in self.adapters.items():
            for item in adapter.enumerate():
                busy = self.adapter is not None and self.adapter.adapter_name == name
                items.append({**item, "busy": busy})
        return items

    async def connect(self, adapter_name: str, device_id: str, project_id: str, owner: str = "camera_setup") -> dict[str, Any]:
        if self.adapter is not None:
            if self.project_id == project_id and getattr(self.adapter, "device_id", None) == device_id and self.state == "ready":
                return self.status()
            await self.disconnect()
        adapter = self.adapters.get(adapter_name)
        if adapter is None:
            raise AppError("CAMERA_ADAPTER_UNSUPPORTED", "The requested camera adapter is not supported.", status_code=422)
        self.state = "opening"
        info = await adapter.connect(device_id)
        self.adapter = adapter
        self.connection_id = str(uuid.uuid4())
        self.project_id = project_id
        self.owner = owner
        self.state = "waiting_for_frame"
        self._verified_frames = 0
        self._connected_at = time.monotonic()
        self._task = asyncio.create_task(self._acquire(), name="camera-acquisition")
        # Await enough complete monotonic frames to make the response useful while remaining bounded.
        try:
            async with asyncio.timeout(6.0):
                while self._verified_frames < 5:
                    async with self._condition:
                        await self._condition.wait()
        except TimeoutError:
            await self.disconnect()
            raise AppError("CAMERA_FRAME_TIMEOUT", "The camera did not provide five complete frames.", status_code=503, retryable=True)
        self.state = "ready"
        return {**info, **self.status()}

    async def _acquire(self) -> None:
        assert self.adapter is not None
        previous = -1
        try:
            async for frame in self.adapter.frames():
                complete = frame.sequence > previous and frame.width > 0 and frame.height > 0 and len(frame.intrinsic_matrix) == 9 and frame.depth_aligned
                if complete:
                    previous = frame.sequence
                    self.latest_frame = frame
                    self._verified_frames += 1
                    if self.state == "waiting_for_frame":
                        self.state = "verifying"
                    async with self._condition:
                        self._condition.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state = "error"
            if hasattr(self.adapter, "_last_error"):
                setattr(self.adapter, "_last_error", str(exc))

    async def wait_for_frame(self, after_sequence: int = -1, timeout: float = 2.0) -> NormalizedCameraFrame:
        if self.latest_frame is not None and self.latest_frame.sequence > after_sequence:
            return self.latest_frame
        try:
            async with asyncio.timeout(timeout):
                while self.latest_frame is None or self.latest_frame.sequence <= after_sequence:
                    async with self._condition:
                        await self._condition.wait()
        except TimeoutError as exc:
            raise AppError("CAMERA_FRAME_TIMEOUT", "No new camera frame arrived.", status_code=503, retryable=True) from exc
        return self.latest_frame

    def status(self) -> dict[str, Any]:
        health = self.adapter.health() if self.adapter else {"state": "disconnected", "fps": 0.0, "frame_count": 0}
        state = self.state if self.adapter else "disconnected"
        frame = self.latest_frame
        width = int(frame.width) if frame is not None else None
        height = int(frame.height) if frame is not None else None
        intrinsic_matrix = list(frame.intrinsic_matrix) if frame is not None else None
        depth_len = (len(frame.depth_m) // 4 if isinstance(frame.depth_m, bytes) else len(frame.depth_m)) if frame is not None and frame.depth_m is not None else 0
        depth_complete = bool(
            frame is not None
            and frame.depth_aligned
            and frame.depth_m is not None
            and depth_len == frame.width * frame.height
        )
        declared_alignment = str(health.get("depth_alignment") or "")
        return {
            "connection_id": self.connection_id, "project_id": self.project_id, "state": state,
            "owner": self.owner, "intrinsics_source": "record3d_per_frame" if self.adapter else None,
            "connected_seconds": round(time.monotonic() - self._connected_at, 3) if self._connected_at else 0.0,
            "frames_received": int(health.get("frame_count", 0)),
            "rgb_width": width,
            "rgb_height": height,
            "depth_width": width if depth_complete else None,
            "depth_height": height if depth_complete else None,
            "depth_aligned": depth_complete and declared_alignment in {"", "rgb_aligned", "rgb_aligned_nearest"},
            "complete_frame_streak": min(int(health.get("frame_count", 0)), 5) if depth_complete and intrinsic_matrix and np.isfinite(intrinsic_matrix).all() else 0,
            "intrinsic_matrix": intrinsic_matrix,
            "error": health.get("last_error"),
            **health,
        }

    async def disconnect(self) -> None:
        task, adapter = self._task, self.adapter
        self._task = None
        self.adapter = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if adapter:
            await adapter.disconnect()
        self.connection_id = self.project_id = self.owner = None
        self.latest_frame = None
        self._connected_at = None
        self.state = "disconnected"
