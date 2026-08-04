# ADR 0005: Live tracking quality and temporal thresholds

- Status: Accepted for v1 simulator/replay; hardware evidence required before release claim
- Date: 2026-08-04

## Context

Painting must stop when camera localization or probe pose is not credible. Thresholds must be explicit, recorded and replay-testable rather than hidden in UI or changed ad hoc.

## Decision

A frame is `good` only when all applicable checks pass:

- Camera localization: at least 30 verified inliers and reprojection error at most 3.0 px.
- Probe PnP: at least 4 of 5 marker inliers and reprojection error at most 2.5 px.
- End-to-end frame latency: at most 150 ms.
- Every transform/position is finite, the probe is in front of the camera, and the tip is inside the active map bounds plus the documented small display tolerance.
- Compared with the previous accepted pose, translation is at most 0.05 m per frame and rotation is at most 30 degrees per frame.

Five consecutive rejected frames transition a tracked stream to `lost`; three consecutive good frames transition it to `recovered/tracked`. Until loss is confirmed, quality is `degraded` and painting is paused. Recovery never resumes painting automatically.

Accepted pose smoothing uses a bounded exponential moving average with alpha 0.35 for translation and normalized quaternion interpolation for rotation. Smoothing never turns a rejected observation into an accepted one and resets after a discontinuity/session boundary.

Paint samples additionally require a monotonic device/server timestamp and immutable active revision references. A manually saved low-quality point requires an explicit reason and is flagged; continuous path sampling has no low-quality override.

Threshold/convention version and effective values are stored in each session manifest and paint evidence. V1 exposes diagnostics, not arbitrary threshold tuning in the normal UI.

## Consequences

The defaults prioritize coordinate integrity and can pause painting during brief occlusion or rapid intentional motion. Frame-count hysteresis avoids flicker but its wall-clock duration varies with FPS, which is visible in session metrics.

## Verification

Recorded replay covers every boundary, NaN/behind-camera values, jumps, latency, five-frame loss, three-frame recovery, smoothing reset and paint rejection/override. Hardware studies compare against measured ground truth; changing a threshold requires a new ADR revision and updated golden evidence.
