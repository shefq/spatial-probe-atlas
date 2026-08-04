# Spatial Probe Atlas v1 implementation notes

`ARCHITECTURE.md` remains unchanged and authoritative. These records make the v1 decisions that the architecture requires before implementation and identify validation boundaries.

## Required v1 decisions

- **Point tile layout:** `SPATILE1`, little-endian. Header: 8-byte ASCII magic, `uint16 version=1`, `uint16 flags` (`bit 0 = RGB`), `uint32 point_count`, and six `float32` bounds values (`min_xyz`, `max_xyz`). Each point is `uint16 qx,qy,qz` followed by `uint8 r,g,b`. Quantization is local to the tile bounds. The JSON manifest is version 1, owns checksums and hierarchy, and the authoritative source remains binary little-endian XYZ/RGB PLY.
- **Record3D conversion:** tested adapter package is `record3d==1.4.1`. RGB, depth, and the exact per-frame intrinsic matrix are copied as one callback snapshot into a capacity-one queue. RGB is normalized to RGB8, depth to `float32` metres, and incomplete/non-finite frames do not enter the five-frame readiness streak. ARKit/Record3D pose axes convert to OpenCV camera axes with `T_C_R = diag(1,-1,-1,1)`; the v1 CPU map localizer does not substitute the device pose for reference-map localization.
- **Board definition:** OpenCV ArUco `DICT_4X4_50`, 3 columns by 2 rows, marker length 0.020 m, separation 0.005 m, IDs 0-5 row-major from top-left. Frame B is at board centre, +X printed right, +Y printed up, +Z outward from the printed face. Replay observations are explicitly marked simulated. Record3D observations require actual board detection and reference-map localization or fail without publishing evidence.
- **Compute profiles:** replay uses deterministic `depth_assisted_replay_v1`. Record3D/import CPU mapping uses SIFT, ratio matching, essential-matrix pose recovery, triangulation, validation, PLY, and tiles (`cpu_sift`). The architecture's pinned pycolmap coordinator and ALIKED/LightGlue CUDA profile remain selectable only when their verified runtime/models are present; this source build returns a typed unavailable error for explicit CUDA instead of pretending CPU work was CUDA. Auto remains CPU-correct.
- **Tracking thresholds:** camera localization requires at least 30 inliers and RMS reprojection error at most 3.0 px. Probe tracking requires at least 4/5 PnP inliers and error at most 2.5 px. End-to-end latency must not exceed 150 ms. A pose jump above 0.05 m/frame or 30 degrees/frame is rejected. Pose translation/rotation filtering uses alpha 0.35. Lost is entered after 5 rejected frames and recovery requires 3 consecutive good frames.
- **Telemetry retention:** paint records are always retained. Bounded diagnostic tracking telemetry is sampled at 2 Hz; high-rate tracking frames remain ephemeral.

## Architecture resolutions and validation boundaries

- Heavy mapping executes in a spawned Python worker with immutable JSON specification, atomic progress/result files, cooperative cancellation, and process termination fallback. The API/job coordinator remains responsive and recovers durable checkpoints after restart.
- Sessions keep immutable map, probe-calibration, and registration IDs. Re-registration never rewrites committed map-frame paint coordinates.
- Portable calibration import is validate-then-import. Blob settings live inside every complete probe calibration revision; Cancel/close on the tuning WebSocket never writes.
- Real Record3D probe calibration never uses replay RMS constants. It validates the known five-marker geometry across real PnP views and reports measured reprojection statistics, or returns a typed insufficient/high-error failure. Replay constants are marked as simulated provenance.
- CUDA and Record3D hardware-in-loop behavior cannot be certified without tagged hardware. Startup and all replay/CPU paths remain functional when optional CUDA/pycolmap/model packages are absent.
- The source dependency file contains exact direct pins. A release ZIP is not acceptable until tooling regenerates a complete transitive `--require-hashes` lock and verifies model/runtime manifests; this is a release-packaging gate, not a runtime fallback.

## Bug fixes applied 2026-08-04

### Structured logging disabled after Alembic migration (2 failing tests)
**Root cause**: `backend/migrations/env.py` called `logging.config.fileConfig(config.config_file_name)` with the default `disable_existing_loggers=True`. This caused Alembic to reset the `spatial_probe_atlas` logger's handlers to `[]` and set `propagate=True` on every programmatic call to `upgrade_database()`. Because `configure_logging()` is called in the FastAPI lifespan **after** `database.migrate()`, it correctly re-added the `RotatingFileHandler`; but the earlier test that called `configure_logging()` directly (without a preceding migration) was polluted when a *different* test's lifespan ran migrate and wiped the global logger state.

**Fix**: Added `disable_existing_loggers=False` to the `fileConfig` call in `env.py`. This preserves any previously-configured logger handlers when Alembic runs programmatically, while still configuring the Alembic-specific console handler for CLI use.

**Files changed**: `backend/migrations/env.py`

### Data-root migration overlap check missed ancestor destination (1 failing test)
**Root cause**: The overlap guard in `native_contract.py` compared only `target = (destination / "SpatialProbeAtlas").resolve()` against `source = data_root.resolve()`. When the caller supplied `destination = data_root.parent`, `target` and `source` are siblings (neither is a parent/child of the other), so the check passed and the migration was incorrectly enqueued.

**Fix**: Extended the condition to also reject any `destination` that is equal to, or an ancestor/descendant of, `source`, regardless of the `SpatialProbeAtlas` sub-path.

**Files changed**: `backend/src/spatial_probe_atlas/api/native_contract.py`

