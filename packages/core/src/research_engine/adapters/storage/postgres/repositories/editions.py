"""Bibliographic identities — one row per edition key seen at ingest."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import editions
from research_engine.domain.works import Edition

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGEditionRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, edition_id: UUID) -> Edition | None:
        """The edition row, for comparing an item's identity to its span's."""
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    editions.select().where(editions.c.id == edition_id)
                )
            ).first()
            return self._to_domain(row) if row else None

    async def get_by_key(self, edition_key: str) -> Edition | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    editions.select().where(editions.c.edition_key == edition_key)
                )
            ).first()
            return self._to_domain(row) if row else None

    async def upsert_key(
        self, tx: Transaction, edition_key: str, csl: dict[str, Any] | None = None
    ) -> Edition:
        """The row for this key, creating it and refreshing its CSL when given."""
        stmt = pg_insert(editions).values(id=uuid7(), edition_key=edition_key)
        if csl is not None:
            stmt = stmt.on_conflict_do_update(
                index_elements=[editions.c.edition_key],
                set_={"csl": csl},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[editions.c.edition_key]
            )
        await tx.conn.execute(stmt)
        row = (
            await tx.conn.execute(
                editions.select().where(editions.c.edition_key == edition_key)
            )
        ).first()
        assert row is not None  # just inserted, or it was already there
        return self._to_domain(row)

    async def list_keys(self) -> list[str]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(editions.c.edition_key).order_by(editions.c.edition_key)
                )
            ).all()
        return [row[0] for row in rows]

    @staticmethod
    def _to_domain(row: Any) -> Edition:
        return Edition(
            id=row.id,
            edition_key=row.edition_key,
            csl=dict(row.csl or {}),
            created_at=row.created_at,
        )
