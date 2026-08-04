# ADR 0007: CUDA runtime and model verification

- Status: Accepted
- Date: 2026-08-04

## Context

The architecture fixes the learned mapping algorithm but leaves the first release's exact CUDA matrix and resource floor to implementation. Selecting CUDA from an NVIDIA driver alone would mislabel work when PyTorch, the device architecture, VRAM, kernel execution, pycolmap, or model assets are unusable.

## Decision

`cuda_aliked_lightglue_v1` is the only v1 learned profile. Its release matrix is Python 3.11, PyTorch `2.11.0+cu128`, Kornia `0.8.3`, and pycolmap `4.1.1`. The CUDA 12.8 Windows driver floor is `570.65`. A device needs at least 4 GiB total VRAM, must be represented in PyTorch's compiled architecture list, and must pass an allocation, multiply/reduce kernel, synchronization, and readback.

The pipeline uses Kornia's released ALIKED `aliked-n16` implementation with at most 4,096 keypoints and LightGlue ALIKED weights with depth confidence 0.95, width confidence 0.99, nine attention layers, and threshold 0.1. Reconstruction uses pycolmap with an explicit PINHOLE camera for every frame and the exact captured `K`. Pairing is bounded exhaustive through 40 frames; larger sets combine an eight-frame sequence window and colour-histogram retrieval top 20.

The ALIKED checkpoint is pinned to upstream commit `683d7c65197395c0b3f01ebe76e1084a27e73a65`; the LightGlue checkpoint is pinned to release `v0.1_arxiv`. URLs, byte sizes, licenses, and SHA-256 values live in `models/manifest.json` and compiled profile metadata. Both must agree and local bytes must verify before `torch.load(weights_only=True)`.

Capability states are `cuda_ready`, `cuda_driver_only`, `cuda_incompatible`, `cpu_only`, or `degraded`. `auto` uses learned mapping only for `cuda_ready`. An explicit unavailable CUDA request returns a typed retryable error. CUDA out-of-memory ends that immutable attempt and advertises a new `cpu_sift_v1` retry; it never changes algorithms mid-job.

## Consequences

CPU-only machines remain fully runnable and the CUDA smoke test skips successfully unless `--require-ready` is used by a CUDA-selected setup. CUDA locks are larger and must come from the immutable PyTorch CUDA 12.8 wheel index. Hardware quality/performance remains a tagged validation item and is never inferred from CPU/replay tests.
