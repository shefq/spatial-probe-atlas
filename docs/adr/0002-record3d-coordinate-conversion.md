# ADR 0002: Record3D to canonical camera conversion

- Status: Accepted, pending hardware matrix evidence
- Date: 2026-08-04

## Context

Record3D/ARKit camera pose axes and the OpenCV camera frame used by intrinsics, PnP and mapping differ. An implicit or viewer-specific flip would corrupt transform composition.

## Decision

The Record3D adapter treats SDK pose frame R as right-handed `+X right, +Y up, -Z viewing direction`. Canonical camera frame C is OpenCV `+X image right, +Y image down, +Z forward`. The adapter applies the row-major rigid transform:

```text
T_C_R = [ 1,  0,  0, 0,
          0, -1,  0, 0,
          0,  0, -1, 0,
          0,  0,  0, 1 ]
```

RGB/depth pixels and per-frame intrinsic matrix `K` already use the image convention and are not mirrored. RGB is normalized to contiguous RGB8, depth to aligned float32 metres, and invalid/non-positive depth becomes missing. The adapter emits `T_C_R` as provenance and no R-frame value leaves it unlabelled.

The implementation is pinned to the tested `record3d==1.4.1` callback contract. A changed SDK axis/pose contract requires a new adapter-version decision and hardware evidence; heuristic auto-detection is rejected.

## Consequences

All domain tracking uses one camera convention and composes `T_W_P = T_W_C T_C_M T_M_P` without Three.js axis knowledge. The viewer applies only the separately fixed W-to-V display conversion.

## Verification

Unit tests transform R basis vectors and round-trip random rigid poses. Hardware validation uses a board moved right/down/forward in the image and a known camera pose to verify signs, handedness, depth scale and `K` at the exact streamed resolution.
