"""Small deterministic replay check used by doctor and setup."""

from __future__ import annotations

import asyncio
import math

from spatial_probe_atlas.adapters.camera.replay import ReplayCameraAdapter


async def _capture() -> list[object]:
    adapter = ReplayCameraAdapter(width=48, height=36, fps=1_000.0)
    devices = adapter.enumerate()
    assert len(devices) == 1 and devices[0]["device_id"] == "replay:synthetic"
    await adapter.connect("replay:synthetic")
    frames = [await adapter.next_frame() for _ in range(5)]
    health = adapter.health()
    await adapter.disconnect()
    assert health["state"] == "ready" and health["frame_count"] == 5
    return frames


async def main() -> None:
    first, second = await asyncio.gather(_capture(), _capture())
    for index, (left, right) in enumerate(zip(first, second, strict=True)):
        assert left.sequence == index == right.sequence
        assert left.width == right.width == 48 and left.height == right.height == 36
        assert left.rgb == right.rgb
        assert left.depth_m == right.depth_m
        assert len(left.rgb) == left.width * left.height * 3
        assert left.depth_m is not None and len(left.depth_m) == left.width * left.height
        assert all(math.isfinite(value) and value > 0 for value in left.depth_m)
        k = left.intrinsic_matrix
        assert len(k) == 9 and k[0] > 0 and k[4] > 0 and k[8] == 1.0
        assert left.device_timestamp_ns <= left.server_timestamp_ns
    print("5 deterministic complete replay frames passed")


if __name__ == "__main__":
    asyncio.run(main())
