from __future__ import annotations

import argparse

from sqlalchemy import inspect

from spatial_probe_atlas.adapters.persistence.database import Base, Database
from spatial_probe_atlas.migration_runtime import database_migration_state
from spatial_probe_atlas.settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or check Spatial Probe Atlas SQLite migrations")
    parser.add_argument("--check", action="store_true", help="Check migration and integrity state without changing it")
    arguments = parser.parse_args(argv)
    settings = Settings.from_env()
    settings.ensure_directories()
    database = Database(settings.database_url)
    try:
        if not arguments.check:
            database.migrate()
            state = database_migration_state(database.engine)
            print(f"SUCCESS: database schema is at Alembic revision {state.head_revision}")
            return 0

        expected = set(Base.metadata.tables)
        present = set(inspect(database.engine).get_table_names())
        missing = sorted(expected - present)
        if missing:
            print(f"FAIL: pending schema tables: {', '.join(missing)}")
            return 2
        state = database_migration_state(database.engine)
        if not state.current:
            print(
                "FAIL: pending Alembic migration "
                f"(current={state.current_revision or 'none'}, head={state.head_revision})"
            )
            return 3
        if database.integrity_check() != "ok":
            print("FAIL: SQLite integrity check did not return ok")
            return 4
        print(f"PASS: database schema, revision {state.head_revision}, and integrity are current")
        return 0
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
