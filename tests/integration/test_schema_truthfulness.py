"""`schema.py` must describe the database that migrations actually build.

Four declared indexes turned out not to exist: three GIN indexes on `json`
columns, which Postgres rejects outright, and one plain btree that migration 001
simply omitted. A declaration nobody can build is worse than no declaration —
it tells a reader that a query is indexed when it is not, and it makes
`metadata.create_all` fail, which is why the test-database helper could not
provision a schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from research_engine.adapters.storage.postgres.schema import metadata

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration]


def declared_schemas() -> set[str]:
    """Every schema `schema.py` declares a table in (`core` by default)."""
    return {table.schema or "core" for table in metadata.tables.values()}


def declared_indexes() -> set[tuple[str, str]]:
    return {
        (table.schema or "core", index.name)
        for table in metadata.tables.values()
        for index in table.indexes
        if index.name
    }


async def actual_indexes(engine: AsyncEngine) -> set[tuple[str, str]]:
    schemas = sorted(declared_schemas())
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT schemaname, indexname FROM pg_indexes "
                "WHERE schemaname = ANY(:schemas)"
            ),
            {"schemas": schemas},
        )
    return {(row[0], row[1]) for row in rows}


async def test_every_declared_index_exists(engine: AsyncEngine) -> None:
    missing = declared_indexes() - await actual_indexes(engine)
    assert not missing, (
        f"schema.py declares indexes the database does not have: {sorted(missing)}. "
        f"Either add a migration that creates them, or remove the declaration — "
        f"a phantom index misleads every reader of this file."
    )
