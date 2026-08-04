# Hardware-dependent validation

Automated CPU/replay success does not validate physical hardware. Record the Windows build, iPhone/iOS/Record3D version, SDK package, cable, GPU, driver and app commit for every run.

## Record3D hardware-in-loop checklist

- [ ] Enumerate exactly the expected device without taking over an active owner.
- [ ] Connect and receive five complete, monotonic RGB/depth/intrinsics frames.
- [ ] Validate exact RGB/depth dimensions, alignment declaration, finite `K`, depth metres and canonical axis conversion.
- [ ] Sustain the target stream while recording FPS, latency, incomplete/drop counts and memory.
- [ ] Unplug/replug, iPhone sleep/unlock, Record3D stop/restart and Windows trust/busy states produce actionable transitions.
- [ ] A second acquisition owner is rejected; disconnect releases the SDK handle once.
- [ ] Capture/import, CPU map, probe tuning/test and live session operate against the same normalized frame contract.

## CUDA checklist

- [ ] Record `nvidia-smi`, driver, device, compute capability and VRAM.
- [ ] Verify the pinned PyTorch CUDA build and a tiny allocation/kernel/result.
- [ ] Run the curated mapping dataset and compare registered ratio, point count, reprojection and coordinate consistency with CPU bounds.
- [ ] Force CUDA OOM and verify a clean CPU retry is offered; no mid-job silent substitution occurs.
- [ ] Confirm VRAM warnings and viewer point-budget reduction before allocation failure.

## Probe/registration/live physical study

- [ ] Confirm the five physical marker coordinates, marker-frame handedness and tip transform against a measured fixture.
- [ ] Validate blob diagnostics across expected exposure, distance, rotation and partial occlusion.
- [ ] Validate board print scale, dictionary/IDs, board-frame axes and held-out registration residuals.
- [ ] Measure tracking quality thresholds, temporal jump limits and localization/probe errors against ground truth.
- [ ] Complete pause/reconnect/recovery, point/path painting and exported map-frame coordinates.

Put signed test records outside the source repository. Failures update the relevant ADR acceptance evidence; they must not be hidden by widening thresholds without review.
