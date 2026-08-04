# Browser E2E tests

These tests exercise the built React application through the same FastAPI loopback origin used in production. They use a disposable data root and synthetic/replay paths; no Record3D device or CUDA is claimed.

Install the developer-only local package and Chromium once:

```powershell
& npm.cmd --prefix tests\e2e ci --no-audit --no-fund
& npm.cmd --prefix tests\e2e run install:browser
```

After normal `setup.bat`, run:

```powershell
& .\scripts\verify.ps1 -E2E
```

`scripts/run_e2e.ps1` applies migrations to a unique `%TEMP%` data root, starts the production backend on an available loopback port, waits for readiness, runs Playwright, terminates only that owned process, and removes only its validated temporary directory. Failure artifacts remain under `tests/e2e/test-results` and `playwright-report`.

Do not point E2E variables at a real Spatial Probe Atlas data root.
