# Spatial Probe Atlas v1 user guide

Spatial Probe Atlas is a local Windows application for Record3D scene mapping, reusable five-marker probe calibration, metric registration, live probe-tip painting, and session review/export. It does not upload project data and does not provide diagnosis or medical-device functionality.

## Install and start

Requirements are 64-bit Windows 10/11, local disk space, and internet access during setup unless a release includes offline assets. Administrator rights, Docker, and global packages are not required.

1. Extract or clone the complete repository to a local directory.
2. Double-click `setup.bat`, or run it from Command Prompt. If Python 3.11 or Node 22 is not already compatible, approve the pinned user-local download when asked. For unattended setup, use `setup.bat -NonInteractive -AcceptRuntimeDownloads`.
3. When setup reports `SUCCESS`, run `run.bat`.
4. Keep the run console open. The app opens at a loopback URL such as `http://127.0.0.1:8765`.
5. Use Ctrl+C in the run console to request a graceful shutdown.

Setup installs Python packages into `.venv`, Node into `.runtime` only when needed, runs `npm ci`, builds the frontend, verifies assets, migrates the local database, and runs smoke diagnostics. A failed setup is restartable.

## First project workflow

1. Create a project on **Projects & Sessions**.
2. On **Camera Setup**, connect one Record3D device and wait for five complete frames to verify RGB, depth, intrinsics and timing. Record3D per-frame intrinsics are authoritative. External-camera calibration is import-only.
3. On **Scene Capture & Mapping**, capture or import a sufficient frame set, build the CPU or verified CUDA map, inspect its point cloud, and activate it.
4. On **Probe & Registration**, create or import a complete `probe_calibration.json`. If five blobs are not tracked, open **Can’t track the probe?**, tune the draft live, and explicitly save a revision. Then solve and validate metric board/tissue registration.
5. On **Live Tissue Painting**, pass preflight, start a session, save points or sampled paths, and finalize it. Tracking loss pauses painting rather than inventing coordinates.
6. On **Session Review & Export**, filter and annotate records, soft-delete/restore mistakes, replay the session, and create checksum-recorded CSV or JSON exports.

## Local data

The default data root is `%LOCALAPPDATA%\SpatialProbeAtlas`. It contains `app.db`, project artifacts, models, caches, logs, temporary job staging and support reports. Runtime project data is never placed under the source repository. Override the root for development by copying `.env.example` to `.env.local` and changing `SPA_DATA_ROOT` to a writable local path.

Back up the entire data root only after closing the application. See `docs/BACKUP_AND_RECOVERY.md` for integrity checks and recovery rules.

## Diagnostics

Run `doctor.bat` whenever startup, camera, GPU, mapping or storage behavior is unclear. Doctor is non-destructive and reports `PASS`, `WARN`, `FAIL` or `SKIP` for runtimes, packages, build, schemas, migration presence, disk/RAM, CUDA, Record3D, model checksums, ports, database integrity and replay.

Useful variants:

```bat
doctor.bat
doctor.bat -CpuMapping
doctor.bat -NoJson
```

CUDA absence is a warning. CPU mode is supported and correct, although reconstruction may take longer. Record3D absence affects live capture but not replay/import workflows.

## Common recovery actions

- Missing build, environment or package: rerun `setup.bat`.
- App already running: `run.bat` opens the healthy existing instance rather than starting another writer.
- Camera not found: unlock the iPhone, open Record3D, verify cable/trust, and make sure another app is not using it.
- Interrupted job: reopen the project and resume only when the UI reports a validated checkpoint.
- Low disk: stop creating maps, archive or remove unneeded exports through the UI, and keep the required job reserve.
- CUDA error or out-of-memory: choose CPU and retry as a clean recorded attempt.
- WebGL loss: accept the reduced point budget or restart the browser.
- Database integrity failure: do not delete or overwrite `app.db`; follow the read-only recovery procedure.

## Updates

Close the application and ensure no active session or job remains. A local release requires its trusted SHA-256 or adjacent `.sha256` sidecar:

```bat
update.bat -ReleasePackage C:\Downloads\spatial-probe-atlas-v1.zip -Sha256 <64-hex-value>
```

The updater verifies every release-manifest file, creates metadata/database backups, retains the prior environment and frontend build for rollback, applies migrations and reruns setup health checks. It refuses active work.
