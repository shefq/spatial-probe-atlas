"""Cross-language JSON/OpenAPI contract smoke checks."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from jsonschema.validators import validator_for


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    schema_paths = sorted((root / "schemas").rglob("*.json"))
    if len(schema_paths) < 3:
        raise SystemExit("expected at least three portable schemas")
    for path in schema_paths:
        schema = json.loads(path.read_text(encoding="utf-8"))
        validator = validator_for(schema)
        validator.check_schema(schema)

    previous_data_root = os.environ.get("SPA_DATA_ROOT")
    previous_token = os.environ.get("SPA_BOOTSTRAP_TOKEN")
    try:
        with tempfile.TemporaryDirectory(prefix="spatial-probe-atlas-contract-") as data_root:
            os.environ["SPA_DATA_ROOT"] = data_root
            os.environ["SPA_BOOTSTRAP_TOKEN"] = "contract-generation-only"
            from spatial_probe_atlas.main import create_app

            app = create_app()
            try:
                openapi = app.openapi()
                assert openapi["info"]["version"] == "1.0.0"
                paths = openapi.get("paths", {})
                required = ("/api/v1/health/live", "/api/v1/health/ready", "/api/v1/projects")
                missing = [path for path in required if path not in paths]
                if missing:
                    raise AssertionError(f"OpenAPI missing required paths: {missing}")
            finally:
                # This verifier does not enter ASGI lifespan, so close its SQLite pool
                # explicitly before TemporaryDirectory attempts Windows cleanup.
                app.state.container.database.engine.dispose()
    finally:
        if previous_data_root is None:
            os.environ.pop("SPA_DATA_ROOT", None)
        else:
            os.environ["SPA_DATA_ROOT"] = previous_data_root
        if previous_token is None:
            os.environ.pop("SPA_BOOTSTRAP_TOKEN", None)
        else:
            os.environ["SPA_BOOTSTRAP_TOKEN"] = previous_token
    print(f"{len(schema_paths)} schemas and required OpenAPI paths passed")


if __name__ == "__main__":
    main()
