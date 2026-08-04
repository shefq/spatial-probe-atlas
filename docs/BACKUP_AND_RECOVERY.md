# Backup and recovery

## Routine backup

1. Finalize or stop any live session and wait for jobs to finish or cancel.
2. Close the application with Ctrl+C and confirm the run console exits.
3. Run `doctor.bat` and require database integrity `PASS`.
4. Copy the entire `%LOCALAPPDATA%\SpatialProbeAtlas` data root to another local volume while the app is stopped.
5. Preserve relative paths and verify a SHA-256 inventory of copied files.

Copying only `app.db` is insufficient: maps, frames, calibrations, paths and exports include immutable file artifacts referenced by relative URI and checksum.

## Database integrity failure

Never delete, rename over, or silently recreate the original `app.db`.

1. Stop the application and make a byte-for-byte copy of the data root.
2. Preserve logs and the latest doctor report.
3. Open only the application’s read-only recovery path.
4. Create an SQLite online backup if the database can still be opened.
5. Run repair/reindex as a non-destructive job that reads project manifests and writes a new candidate database/artifact index.
6. Validate counts, relationships, immutable revision references and checksums before explicitly selecting the repaired copy.

## Interrupted jobs

On startup, processing jobs become `interrupted`. Resume is allowed only when input/settings/checkpoint checksums match. Published artifacts remain untouched; invalid staging is retained for bounded diagnostics and a clean retry creates a new attempt.

## Restore validation

Restore into a separate data root first. Run doctor, database integrity, manifest/checksum verification, project summaries, map tile loading and session export comparison before replacing the normal root. Project schema downgrade is unsupported.
