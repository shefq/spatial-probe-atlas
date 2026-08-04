# ADR 0003: V1 registration board definition

- Status: Accepted, pending print and hardware validation
- Date: 2026-08-04

## Context

Metric registration must not depend on an ambiguous printed board, dictionary, marker ordering or axis convention.

## Decision

V1 has one built-in OpenCV ArUco GridBoard definition:

- Dictionary: `DICT_4X4_50`.
- Grid: 3 columns by 2 rows.
- Marker IDs: 0 through 5 in row-major order starting at the printed top-left.
- Marker black-square side length: 0.020 m.
- Clear separation between adjacent marker squares: 0.005 m.
- Frame B origin: geometric centre of the full marker grid.
- Viewed from the printed/front surface: `+X` points printed right, `+Y` points printed up and `+Z` points outward toward the camera.
- Units: metres; printed pages must not be scaled by a PDF/printer fit option.

The board definition, dictionary, dimensions, ordered IDs and convention version are stored with every observation and immutable registration revision. Detection estimates `T_C_B`; it never infers dimensions from pixels. A different board is incompatible data, not a tuning option in v1.

At least three non-degenerate board/map observations are required to solve; one or more separately held-out observations validate scale and pose. Registration activation always records RMS/max residual and `passed` or explicit `accepted_with_warning` status.

## Consequences

A fixed inexpensive board makes scale/axes reproducible and avoids generic board-designer UI. Damaged, incorrectly scaled or partially cropped prints must be replaced rather than compensated by arbitrary parameters.

## Verification

The shipped board asset is machine-checked for IDs/layout and includes a measured reference ruler. Synthetic projection tests validate corners, board centre and axis signs. Hardware tests measure printed marker/separation dimensions, pose a board at known offsets and use held-out observations for residual/scale evidence.
