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


def declared_tables() -> set[tuple[str, str]]:
    return {(table.schema or "core", table.name) for table in metadata.tables.values()}


async def actual_indexes_by_table(engine: AsyncEngine) -> set[tuple[str, str, str]]:
    schemas = sorted(declared_schemas())
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT schemaname, tablename, indexname FROM pg_indexes "
                "WHERE schemaname = ANY(:schemas)"
            ),
            {"schemas": schemas},
        )
    return {(row[0], row[1], row[2]) for row in rows}


async def test_every_index_the_database_has_is_declared(engine: AsyncEngine) -> None:
    """The other direction, which is the one that caught nothing for a year.

    `declared - actual` finds a declaration nobody can build. It cannot find an
    index the database has *lost*, because a lost index that was never declared
    is missing from both sides and subtracts to nothing. That is exactly what
    happened to `passage_embeddings_hnsw`: migration 006 built it, something
    later dropped it, `schema.py` declared the column as `Vector()` with no
    dimension and no index at all, and every semantic search quietly went back
    to a sequential scan with no test able to say so.

    Asserting `actual - declared` closes it from the other side: an index that
    exists must be described, so the description is complete enough for the
    first test to police. Constraint-backed indexes (primary keys, unique
    constraints) are excluded — SQLAlchemy models those as constraints rather
    than as `Table.indexes`, so they are declared, just not there.
    """
    declared = declared_indexes()
    constraint_backed = {
        (table.schema or "core", index_name)
        for table in metadata.tables.values()
        for index_name in _constraint_index_names(table)
    }
    undeclared = {
        (schema, index)
        for schema, table, index in await actual_indexes_by_table(engine)
        # Only tables schema.py claims to describe; a pack's own tables in these
        # schemas are not this file's business.
        if (schema, table) in declared_tables()
    } - declared - constraint_backed

    assert not undeclared, (
        f"the database has indexes schema.py does not declare: {sorted(undeclared)}. "
        f"Declare them — an index nobody has written down is one nobody will "
        f"notice the loss of."
    )


def _constraint_index_names(table: sa.Table) -> set[str]:
    """Index names Postgres creates to back a primary key or unique constraint."""
    names = set()
    if table.primary_key is not None and table.primary_key.columns:
        names.add(table.primary_key.name or f"{table.name}_pkey")
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint):
            names.add(constraint.name or _default_unique_name(table, constraint))
    return names


def _default_unique_name(table: sa.Table, constraint: sa.UniqueConstraint) -> str:
    """Postgres names an unnamed unique constraint `<table>_<cols>_key`."""
    return f"{table.name}_{'_'.join(c.name for c in constraint.columns)}_key"
