# V1 implementation checklist

This checklist condenses the authoritative phase plan in `ARCHITECTURE.md`. A phase is complete only when its vertical slice and exit verification pass; hardware checks are tracked separately from automated checks.

## Phase 1 — foundation and mapping

- [ ] Deterministic Windows setup/run/doctor, local data root/lock, migrations, static production build, health/resources.
- [ ] Projects and summaries; Record3D plus deterministic replay; capture/import and quality checks.
- [ ] Durable CPU mapping job, validated PLY/basic tiles, activation, mapping viewer.
- [ ] Verify clean setup, API/frontend tests, replay capture, CPU map publication and inspection.

## Phase 2 — calibration and registration

- [ ] Optional verified CUDA profile with explicit CPU fallback; durable job cancel/recovery.
- [ ] Versioned complete probe calibration validation/import/revisions/download; capture/optimization/tuning/test.
- [ ] ArUco board observations, scale/registration solve and residual validation; registration viewer.
- [ ] Legacy staged importer, stronger LOD/resource warnings, repeated calibration/metric-registration verification.

## Phase 3 — live tracking and painting

- [ ] Reference localization, probe PnP/quality states, direct viewer stream and instrumentation.
- [ ] Immutable-revision preflight; recoverable session lifecycle and reconnect behavior.
- [ ] Quality-gated points/paths, time/distance sampling, chunks, persistence and undo.
- [ ] Verify deterministic recorded-session replay and recoverable end-to-end live session without hardware.

## Phase 4 — review, export and hardening

- [ ] Paged/filterable review, replay, annotation, soft delete/restore.
- [ ] Reproducible CSV/JSON/manifests/checksums and supported image/point-overlay exports only.
- [ ] Support bundle, repair/reindex, backup/restore documentation.
- [ ] Verify full browser workflow, installer/doctor paths and exports; record unrun Record3D/CUDA hardware checks.

## Release gates

- [ ] No v1 excluded sensor, diagnosis, mesh/GLB, splatting, cloud, multi-user or plugin-runtime features.
- [ ] CPU correctness does not depend on CUDA or physical hardware.
- [ ] All committed dependency/runtime/model assets are exact and checksum-verifiable.
- [ ] `setup.bat`, `run.bat`, `doctor.bat`, automated verification and user documentation agree.
