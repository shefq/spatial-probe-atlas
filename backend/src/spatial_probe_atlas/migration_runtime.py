from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory


@dataclass(frozen=True, slots=True)
class MigrationState:
    current_revision: str | None
    head_revision: str

    @property
    def current(self) -> bool:
        return self.current_revision == self.head_revision


def _config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_database(engine: Any) -> MigrationState:
    config = _config(str(engine.url))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    return database_migration_state(engine)


def database_migration_state(engine: Any) -> MigrationState:
    config = _config(str(engine.url))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise RuntimeError("The Alembic migration directory has no head revision.")
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return MigrationState(current_revision=current, head_revision=head)
