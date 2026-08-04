from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder

from spatial_probe_atlas.adapters.persistence.database import IdempotencyRecord

from .strict_catalog import Catalog as StrictCatalog


class Catalog(StrictCatalog):
    """Strict catalog with JSON-safe durable idempotency snapshots."""

    def save_idempotent_response(self, scope: str, key: str | None, response: dict[str, Any]) -> None:
        if not key:
            return
        with self.database.session() as session:
            session.merge(IdempotencyRecord(key=key, scope=scope, response=jsonable_encoder(response)))
