# ADR 0004: Mapping compute profiles

- Status: Accepted
- Date: 2026-08-04

## Context

GPU acceleration is optional. Reproducibility requires named configurations and forbids silently replacing an algorithm halfway through a job.

## Decision

V1 publishes two production profiles and one fixture-only profile:

### `cpu_sift_v1`

- OpenCV SIFT, maximum 8,192 features per image, contrast threshold 0.04, edge threshold 10, sigma 1.6.
- Sequential pairs within 8 frames plus retrieval top 20; bounded exhaustive pairing when a capture set has at most 40 frames.
- L2 two-nearest-neighbour matching, Lowe ratio 0.75, mutual consistency, at least 15 verified inliers.
- pycolmap/COLMAP reconstruction with compatible per-resolution intrinsic groups; per-frame `K` remains recorded.

### `cuda_aliked_lightglue_v1`

- ALIKED `n16`, maximum 4,096 keypoints per image.
- The same pair policy as CPU so profile comparisons use equivalent candidate connectivity.
- LightGlue ALIKED weights with depth confidence 0.95, width confidence 0.99 and 50 maximum pruning layers/iterations as supported by the pinned integration.
- The same pycolmap/COLMAP reconstruction/validation and output contracts.

CUDA is selected only after driver, PyTorch build/device/capability/VRAM, allocation/kernel and model-checksum checks all pass. CUDA OOM fails that attempt and offers a clean `cpu_sift_v1` retry. It never switches an in-progress attempt.

### `depth_assisted_replay_v1`

This deterministic fixture/simulator profile back-projects normalized replay depth, uses a declared synthetic sequence baseline, and voxel-downsamples at 1 mm. It validates capture/job/PLY/tile/publication plumbing without claiming SfM quality and is never selected for a hardware production reconstruction.

Every job stores requested/effective profile, dependency/model versions, full parameters and input checksums. Validation always checks registered ratio, connected component, finite poses/points, track statistics, reprojection distribution, PLY/tile checksums and publication staging.

## Consequences

CPU correctness remains available on every supported machine. CUDA can be faster but increases lock/model maintenance. Fixture mapping makes CI deterministic but cannot satisfy the Phase 1 hardware mapping exit by itself.

## Verification

Curated CPU/GPU datasets use recorded threshold bands for registered ratio, point count, reprojection error and coordinate consistency. Replay golden bytes validate plumbing. CI must label the production quality suites separately from replay smoke tests.
