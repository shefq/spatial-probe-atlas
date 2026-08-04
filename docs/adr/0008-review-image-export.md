# ADR 0008: deterministic review image exports

- Status: accepted for v1
- Date: 2026-08-04

## Context

The v1 export list includes a screenshot and a point-overlay image. An export job runs on
the server after the browser request and does not own the browser's Three.js render camera,
viewport, loaded map tiles, or GPU buffers. Calling a server-generated image the "current
viewer screenshot" would therefore make the artifact misleading and non-reproducible.

## Decision

Both image formats use the deterministic `server_orthographic_v1` renderer:

- fixed 1024 x 768 output;
- top-down orthographic projection of the W-frame XY plane;
- metres remain the source units;
- fixed margins, aspect-preserving fit, colours, point radius, line width, and draw order;
- the frozen server-side review filters select the records;
- `screenshot` uses a dark grid background;
- `point_overlay` is an RGBA PNG with a transparent background;
- neither image includes the point-cloud map or claims to reproduce the browser camera.

PNG text chunks identify the application version, coordinate frame, units, filter checksum,
source-record checksum, and renderer description. The export resource separately records the
artifact SHA-256 and render metadata. Identical records and filter snapshots produce identical
PNG bytes.

## Consequences

The image exports are reproducible without WebGL or a live browser and remain useful in image
editing and reports. They are intentionally not evidence of a particular interactive viewer
angle. A future browser-camera capture would need a separate, explicit camera-state request and
contract; it is not part of v1.
