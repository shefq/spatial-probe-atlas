# Spatial Probe Atlas

Spatial Probe Atlas is a Windows local-first browser application for Record3D scene mapping, reusable five-marker probe calibration, metric board/tissue registration, live probe-tip painting, and reproducible session review/export.

V1 intentionally does not include spectrometers, FBG sensors, temperature, classification/diagnosis, meshes/GLB, Gaussian splatting, cloud accounts or a plugin runtime.

## Install and run

On 64-bit Windows 10/11:

```bat
setup.bat
run.bat
```

Setup uses only repository-local `.venv`/`.runtime` dependencies, builds the production frontend, applies migrations and runs smoke checks. Normal operation is one FastAPI process serving the built UI and same-origin API/WebSockets on `127.0.0.1`. Keep the run console open and use Ctrl+C for graceful shutdown.

If setup needs compatible runtimes in an unattended environment:

```bat
setup.bat -NonInteractive -AcceptRuntimeDownloads
```

Run non-destructive diagnostics with:

```bat
doctor.bat
doctor.bat -CpuMapping
```

The default data root is `%LOCALAPPDATA%\SpatialProbeAtlas`; project data is never stored in this repository. See [docs/USER_GUIDE.md](docs/USER_GUIDE.md), [docs/BACKUP_AND_RECOVERY.md](docs/BACKUP_AND_RECOVERY.md), and [docs/HARDWARE_VALIDATION.md](docs/HARDWARE_VALIDATION.md).

## Development verification

After setup:

```powershell
& .\scripts\verify.ps1 -CpuMapping
```

Browser E2E dependencies and commands are in [tests/e2e/README.md](tests/e2e/README.md). Record3D and CUDA checks are explicitly separate from deterministic CPU/replay automation. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) and the required decisions under [docs/adr](docs/adr).

`ARCHITECTURE.md` is the authoritative v1 product and technical contract.
