"""Build and validate one deterministic hardware-free point cloud."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import uuid
from pathlib import Path

import numpy as np

from spatial_probe_atlas.adapters.camera.replay import ReplayCameraAdapter
from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.pipelines.mapping.cpu import build_cpu_point_cloud


async def _frames(store: ArtifactStore, project_id: str) -> list[dict[str, object]]:
    adapter = ReplayCameraAdapter(width=80, height=60, fps=1_000.0)
    await adapter.connect("replay:synthetic")
    payloads: list[dict[str, object]] = []
    for index in range(20):
        frame = await adapter.next_frame()
        frame_root = store.project_path(project_id, Path("captures/smoke") / f"{index:04d}")
        rgb = store.atomic_write_bytes(frame_root / "rgb.raw", frame.rgb)
        depth_bytes = np.asarray(frame.depth_m, dtype="<f4").tobytes()
        depth = store.atomic_write_bytes(frame_root / "depth.f32", depth_bytes)
        payloads.append(
            {
                "width": frame.width,
                "height": frame.height,
                "intrinsic_matrix": list(frame.intrinsic_matrix),
                "rgb_artifact": rgb,
                "depth_artifact": depth,
            }
        )
    await adapter.disconnect()
    return payloads


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="spatial-probe-atlas-map-smoke-") as temporary:
        root = Path(temporary)
        store = ArtifactStore(root)
        project_id, map_id, job_id = (str(uuid.uuid4()) for _ in range(3))
        frames = asyncio.run(_frames(store, project_id))
        events: list[tuple[str, int, int, float, str]] = []
        result = build_cpu_point_cloud(
            store,
            project_id=project_id,
            map_id=map_id,
            job_id=job_id,
            frames=frames,
            progress=lambda *values: events.append(values),
            cancelled=lambda: False,
        )
        map_root = store.project_path(project_id, Path("maps") / map_id)
        ply = map_root / "point-cloud.ply"
        manifest_path = map_root / "manifest.json"
        assert ply.read_bytes().startswith(b"ply\nformat binary_little_endian 1.0\n")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tile = map_root / "tiles" / "r.spatile"
        assert tile.read_bytes().startswith(b"SPATILE1")
        assert hashlib.sha256(tile.read_bytes()).hexdigest() == manifest["tiles"]["r"]["sha256"]
        assert result["point_count"] >= 100 and manifest["point_count"] == result["point_count"]
        assert events[-1][0] == "publish" and events[-1][3] == 1.0
        print(f"CPU replay map passed: {result['point_count']} points, PLY/tile checksums valid")


if __name__ == "__main__":
    main()
