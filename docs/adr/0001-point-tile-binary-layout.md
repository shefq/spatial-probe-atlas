# ADR 0001: Version 1 point-tile binary layout

- Status: Accepted
- Date: 2026-08-04

## Context

The browser needs progressively loadable point data without point-cloud JSON. Binary little-endian PLY remains authoritative; the browser format is a small, versioned derivative that pure Three.js can decode in a worker.

## Decision

Version 1 tiles use the extension `.spatile` and this exact little-endian layout:

| Offset | Type | Meaning |
|---:|---|---|
| 0 | 8 bytes | ASCII magic `SPATILE1` |
| 8 | `uint16` | Format version, `1` |
| 10 | `uint16` | Flags; bit 0 means RGB8 is present; all other bits must be zero |
| 12 | `uint32` | Point count `N` |
| 16 | 6 x `float32` | `min_x,min_y,min_z,max_x,max_y,max_z` in metres in frame W |
| 40 | `N` x 9 bytes | Interleaved `uint16 x,y,z` followed by `uint8 r,g,b` |

Position component `q` decodes as `min + (q / 65535) * (max - min)`. A zero extent decodes to the corresponding minimum. Values are finite before encoding; colour is sRGB byte data. There is no alignment padding between point records.

The JSON octree manifest declares format `spatial-probe-atlas-octree`, version `1`, coordinate frame `W`, units `m`, total/bounds, root IDs and, for every tile, relative URI, bounds, point count, child IDs, geometric error and SHA-256. Tile URIs never contain absolute paths. Immutable responses use ETag from the checksum.

Unknown versions, flags, impossible byte lengths, non-finite bounds, checksum mismatch, point-count overrun or tile bounds outside the declared manifest are rejected before GPU allocation.

## Consequences

The format is compact and trivial to decode, but position precision depends on tile extent. Large roots must split into octree children before visualization-quality precision degrades. Adding normals or other attributes requires a new version/flag contract; v1 readers must not guess.

## Verification

Golden tests compare exact bytes, decoded basis/extreme points, zero extents, malformed headers, checksum rejection and encode/decode error. Viewer tests verify worker transfer and deterministic GPU-buffer disposal.
