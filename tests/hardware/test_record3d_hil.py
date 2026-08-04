from __future__ import annotations

import asyncio
import math
import os

import pytest

from spatial_probe_atlas.adapters.camera.record3d import Record3DAdapter


pytestmark = pytest.mark.hardware


def _require_hardware_consent() -> None:
    if os.environ.get("SPA_RUN_HARDWARE_TESTS") != "1":
        pytest.skip("set SPA_RUN_HARDWARE_TESTS=1 for explicit hardware-test consent")


def test_record3d_enumerates_without_connecting() -> None:
    _require_hardware_consent()
    adapter = Record3DAdapter()
    assert adapter.available, "record3d==1.4.1 is not importable"
    devices = adapter.enumerate()
    assert devices, "no unlocked/trusted Record3D device was enumerated"
    assert all(device["adapter"] == "record3d" for device in devices)


@pytest.mark.slow
def test_record3d_complete_monotonic_stream() -> None:
    _require_hardware_consent()
    if os.environ.get("SPA_HARDWARE_ALLOW_CONNECT") != "1":
        pytest.skip("set SPA_HARDWARE_ALLOW_CONNECT=1 to allow exclusive device acquisition")

    async def exercise() -> None:
        adapter = Record3DAdapter()
        devices = adapter.enumerate()
        assert devices
        await adapter.connect(str(devices[0]["device_id"]))
        frames = []
        try:
            iterator = adapter.frames().__aiter__()
            for _ in range(100):
                frames.append(await asyncio.wait_for(iterator.__anext__(), timeout=3.0))
        finally:
            await adapter.disconnect()
        assert [frame.sequence for frame in frames] == list(range(100))
        assert all(frame.device_timestamp_ns <= frame.server_timestamp_ns for frame in frames)
        assert all(frames[index].device_timestamp_ns < frames[index + 1].device_timestamp_ns for index in range(99))
        for frame in frames:
            assert frame.width > 0 and frame.height > 0
            assert len(frame.rgb) == frame.width * frame.height * 3
            assert frame.depth_m is not None and len(frame.depth_m) == frame.width * frame.height
            assert len(frame.intrinsic_matrix) == 9
            assert math.isfinite(frame.intrinsic_matrix[0]) and frame.intrinsic_matrix[0] > 0
            assert math.isfinite(frame.intrinsic_matrix[4]) and frame.intrinsic_matrix[4] > 0
            assert frame.depth_aligned

    asyncio.run(exercise())
