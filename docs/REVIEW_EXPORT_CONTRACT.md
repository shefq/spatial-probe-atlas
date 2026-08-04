# Review and export v1 contract

This document records the concrete Phase 4 HTTP behavior implemented under `/api/v1`.
Coordinates are stored in world/map frame `W` and metres.

## Review queries

The following routes return the same cursor-paged envelope:

- `GET /projects/{project_id}/sessions/{session_id}/painted-points`
- `GET /projects/{project_id}/sessions/{session_id}/painted-paths`
- `GET /projects/{project_id}/sessions/{session_id}/painted-records`

Query parameters are `cursor`, `limit` (1-1000), `from`, `to`, `quality`, and
`include_deleted`. The combined route additionally accepts `type`. `from` and `to` are
inclusive RFC 3339 UTC timestamps (`Z` or `+00:00`). Type is `all`, `point`, or `path`.
Quality is `all`, `good`, `warning`, `low`, or `flagged_low_quality`; `low` includes
`flagged_low_quality` records. A path is filtered and ordered by its start timestamp.

```json
{
  "items": [],
  "next_cursor": null,
  "total": 0,
  "filters": {
    "type": "all",
    "quality": "all",
    "from": null,
    "to": null,
    "include_deleted": false
  }
}
```

`total` is the full count for the filter snapshot, not the current page size. Cursors are
opaque, stable chronological `(timestamp, UUID)` positions bound to the normalized filter.
Reusing a cursor with changed filters returns `422 REVIEW_CURSOR_INVALID`.

## Review mutations

Annotation, soft delete, and restore use the item routes documented in `ARCHITECTURE.md`.
When a session is `running`, `paused`, `degraded`, or `stopping`, these commands return
`409 SESSION_REVIEW_READ_ONLY`; the record remains unchanged. Stopped, finalized,
recoverable, and failed sessions may be repaired through review commands.

## Export creation and download

`POST /projects/{project_id}/sessions/{session_id}/exports` accepts:

```json
{
  "format": "json",
  "filters": {
    "type": "all",
    "quality": "all",
    "from": null,
    "to": null,
    "include_deleted": false
  },
  "include_deleted": false
}
```

Formats are exactly `json`, `csv`, `session_manifest`, `screenshot`, and `point_overlay`.
The response is `202` with the export ID, durable job ID, state, and the normalized immutable
filter snapshot. The job always queries using that stored snapshot.

Completed export resources contain `schema_version`, application format, media type, relative
artifact URI, byte size, SHA-256, filter/source checksums, record counts, `W`/`m` declarations,
and image-render metadata when applicable. JSON separates points and paths. CSV contains one
metadata row, point rows, and flattened path-sample rows; every data row carries its own canonical
record checksum. The session manifest contains immutable map, probe-calibration, and registration
IDs/revisions plus content/artifact checksums.

`GET /projects/{project_id}/sessions/{session_id}/exports/{export_id}/download` resolves the
stored relative path inside the data root and recomputes SHA-256 before returning any bytes.
It returns an immutable ETag equal to that checksum. Missing or changed files return a typed
`409` integrity error instead of serving corrupt data.

## Image export meaning

See ADR 0008. `screenshot` and `point_overlay` are deterministic server-rendered top-down W-XY
paint images. They do not include the map and do not reproduce the browser camera. The overlay
has a transparent background; the screenshot has a fixed dark grid. Both embed PNG metadata for
frame, units, renderer, filter checksum, and source checksum.
