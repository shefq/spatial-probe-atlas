# ADR 0006: Session telemetry sampling and retention

- Status: Accepted
- Date: 2026-08-04

## Context

Persisting every high-rate tracking frame would grow sessions rapidly, while paint records alone do not explain tracking loss or performance regressions.

## Decision

V1 persists bounded telemetry at 2 Hz while a session is `running` or `degraded`, and pauses sampling while the session is `paused`. A sample contains timestamps, frame sequence, tracked/lost states, camera/probe inlier counts and reprojection errors, latency/FPS/drop counters, compute mode and quality category. It does not contain RGB/depth imagery or arbitrary full logs.

Paint commands persist their authoritative frame/time/transform/quality evidence independently and are never downsampled by this policy. Paths remain bounded at 2,000 paint samples per chunk.

Telemetry is append-only in bounded chunks, retained with the session until the user explicitly purges that session, and included in project/session size accounting. Review loads summaries first and telemetry only in paged/replay windows. Support bundles include aggregate metrics by default, not session telemetry or images, unless the user explicitly opts in.

If storage admission enters a critical state, painting remains authoritative; telemetry may drop future samples with a recorded counter/warning rather than blocking the camera loop. A rate change creates a versioned setting and is stored in the session manifest.

## Consequences

Two samples per second are sufficient for state/latency timelines without treating SQLite as a frame store. Very brief events may appear only in paint evidence or aggregate counters, so live high-rate diagnosis remains log/performance instrumentation rather than durable session data.

## Verification

Replay tests use a monotonic synthetic clock to prove rate bounds, pause/resume behavior, chunk rollover, deterministic summaries, low-disk drops and immutable paint records. Export manifests state the telemetry schema and configured/effective rate.
