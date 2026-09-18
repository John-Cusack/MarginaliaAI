"""Locate, run, and verify the packaged database migrations."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from research_engine.domain.errors import ConfigurationError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def migration_config() -> Config:
    """Build an Alembic config that works inside an installed wheel."""
    ini = Path(__file__).with_name("alembic.ini")
    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent))
    return config


@lru_cache(maxsize=1)
def expected_heads() -> tuple[str, ...]:
    """Return the migration heads shipped with this installation."""
    return tuple(ScriptDirectory.from_config(migration_config()).get_heads())


async def current_heads(engine: AsyncEngine) -> tuple[str, ...]:
    """Return the revisions recorded in the configured database."""
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: tuple(
                MigrationContext.configure(sync_connection).get_current_heads()
            )
        )


async def require_current_schema(engine: AsyncEngine) -> None:
    """Refuse runtime startup when explicit migrations have not been applied."""
    current = await current_heads(engine)
    expected = expected_heads()
    if frozenset(current) == frozenset(expected):
        return

    current_label = ", ".join(sorted(current)) if current else "uninitialized"
    expected_label = ", ".join(sorted(expected))
    raise ConfigurationError(
        f"Database schema is at {current_label}; this installation expects "
        f"{expected_label}. Run `research-engine db upgrade` with the same "
        "RE_DB_URL, then retry."
    )
