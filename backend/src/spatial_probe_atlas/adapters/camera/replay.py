from __future__ import annotations

import asyncio
import math
import time
from typing import AsyncIterator

from spatial_probe_atlas.ports.camera import NormalizedCameraFrame


class ReplayCameraAdapter:
    """Deterministic hardware-free RGB/depth/intrinsics source for CI and demos."""

    adapter_name = "replay"

    def __init__(self, *, width: int = 160, height: int = 120, fps: float = 20.0) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self._connected = False
        self._sequence = 0
        self._started_ns = 0
        self._dropped = 0

    def enumerate(self) -> list[dict[str, object]]:
        return [{
            "device_id": "replay:synthetic", "adapter": "replay", "name": "Synthetic Record3D Replay",
            "available": True, "hardware": False, "capabilities": ["rgb", "depth", "per_frame_intrinsics", "deterministic"],
        }]

    async def connect(self, device_id: str) -> dict[str, object]:
        if device_id != "replay:synthetic":
            raise ValueError(f"Unknown replay device: {device_id}")
        self._connected = True
        self._sequence = 0
        self._started_ns = time.monotonic_ns()
        return {"device_id": device_id, "adapter": "replay", "width": self.width, "height": self.height, "fps": self.fps}

    def _make_frame(self) -> NormalizedCameraFrame:
        seq = self._sequence
        self._sequence += 1
        width, height = self.width, self.height
        # RGB pattern is deterministic, contains texture for CPU feature/mapping smoke paths,
        # and translates slightly with sequence to provide baseline.
        rgb = bytearray(width * height * 3)
        phase = seq % 37
        for y in range(height):
            row = y * width
            for x in range(width):
                index = (row + x) * 3
                checker = 56 if ((x + phase) // 12 + y // 12) % 2 else 196
                rgb[index] = (checker + x + phase * 3) % 256
                rgb[index + 1] = (checker + y * 2) % 256
                rgb[index + 2] = (x * 3 + y * 5 + phase) % 256
        depth = tuple(0.42 + 0.0008 * x + 0.0004 * y + 0.004 * math.sin((x + phase) / 18) for y in range(height) for x in range(width))
        now = time.monotonic_ns()
        k = (140.0, 0.0, width / 2, 0.0, 140.0, height / 2, 0.0, 0.0, 1.0)
        return NormalizedCameraFrame(seq, now, now, width, height, k, bytes(rgb), depth)

    async def next_frame(self) -> NormalizedCameraFrame:
        if not self._connected:
            raise RuntimeError("Replay camera is not connected")
        await asyncio.sleep(1 / self.fps)
        return self._make_frame()

    async def frames(self) -> AsyncIterator[NormalizedCameraFrame]:
        while self._connected:
            yield await self.next_frame()

    def health(self) -> dict[str, object]:
        return {
            "state": "ready" if self._connected else "disconnected", "fps": self.fps if self._connected else 0.0,
            "frame_count": self._sequence, "dropped_frames": self._dropped, "incomplete_frames": 0,
            "intrinsics_source": "record3d_per_frame", "depth_alignment": "rgb_aligned", "latency_ms": 0.5,
            "resolution": {"width": self.width, "height": self.height},
        }

    async def disconnect(self) -> None:
        self._connected = False

