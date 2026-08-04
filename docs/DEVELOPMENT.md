# Development and verification

`ARCHITECTURE.md` is the product and technical contract. Runtime project data must stay outside this repository, and the legacy `ar_tissue_painting` repository is read-only behavioral/data-format reference only.

## Local environment

Run `setup.bat` once. It creates `.venv`, installs the backend lock, runs `npm ci`, builds `frontend/dist`, applies migrations and writes `.setup-complete.json`. The production path is one FastAPI process serving that build. Vite is developer-only.

For a backend command in PowerShell:

```powershell
$env:PYTHONPATH = "$PWD\backend\src"
& .\.venv\Scripts\python.exe -m pytest -q
```

For frontend development:

```powershell
& npm.cmd --prefix frontend run dev
```

Use `scripts\verify.ps1` for the cross-stack unit/contract/replay/build suite. Use `scripts\verify.ps1 -E2E` only after installing the local Playwright test package documented in `tests/e2e/README.md`. Hardware tests are opt-in and never part of deterministic CPU CI.

## Release discipline

- Support Python 3.11 and Node 22 only for v1; runtime archives are pinned in `tools/runtime-manifest.json`.
- Python release locks must contain the full transitive graph and hashes; source-development exact pins without hashes are not release-grade.
- Commit `frontend/package-lock.json` and use `npm ci`; dependencies have exact versions.
- Model entries require immutable HTTPS URLs, SHA-256 and license metadata before an asset is required.
- Generate frontend API types from backend OpenAPI and fail verification on drift.
- Do not serve a Vite dev server in normal operation or bind to a non-loopback interface.

## Data and migrations

Use a disposable data root for tests:

```powershell
$env:SPA_DATA_ROOT = "$env:TEMP\spa-dev-data"
```

Apply migrations with `python -m spatial_probe_atlas.migrations`; check without mutation using `--check`. Do not point automated tests at a real user data root. Artifacts publish through same-volume staging, validation and atomic rename.

## Verification boundaries

Deterministic automation uses replay/fixtures for camera frames, mapping, tracking and painting. CUDA and Record3D checks are separately marked. A skipped hardware check is reported as unvalidated, never treated as a passing hardware claim.
