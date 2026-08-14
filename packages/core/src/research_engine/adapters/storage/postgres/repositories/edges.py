"""Postgres edge repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import edges
from research_engine.domain.edges import Edge, EdgeDraft

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGEdgeRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, tx: Transaction, draft: EdgeDraft) -> Edge:
        edge_id = uuid7()
        values = {
            "id": edge_id,
            "source_kind": draft.source_kind.value,
            "source_id": draft.source_id,
            "target_kind": draft.target_kind.value,
            "target_id": draft.target_id,
            "relation_type": draft.relation_type,
            "attributes": draft.attributes,
            "source_passage_id": draft.source_passage_id,
            "confidence": draft.confidence,
        }
        await tx.conn.execute(edges.insert().values(**values))
        return await self._get_by_id(tx.conn, edge_id)  # type: ignore[return-value]

    async def upsert(self, tx: Transaction, draft: EdgeDraft) -> Edge:
        """Insert an edge, or update it in place if one already exists with the
        same (source, target, relation_type) triple.

        Dedup relies on the ``edges_natural_key_uq`` unique index. On conflict
        the mutable fields (attributes, confidence, source_passage_id) are
        refreshed so re-running an extraction enriches rather than duplicates.
        """
        values = {
            "id": uuid7(),
            "source_kind": draft.source_kind.value,
            "source_id": draft.source_id,
            "target_kind": draft.target_kind.value,
            "target_id": draft.target_id,
            "relation_type": draft.relation_type,
            "attributes": draft.attributes,
            "source_passage_id": draft.source_passage_id,
            "confidence": draft.confidence,
        }
        stmt = pg_insert(edges).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "source_kind",
                "source_id",
                "target_kind",
                "target_id",
                "relation_type",
            ],
            set_={
                "attributes": stmt.excluded.attributes,
                "confidence": stmt.excluded.confidence,
                "source_passage_id": stmt.excluded.source_passage_id,
            },
        ).returning(edges.c.id)
        edge_id = (await tx.conn.execute(stmt)).scalar_one()
        return await self._get_by_id(tx.conn, edge_id)  # type: ignore[return-value]

    async def get(self, edge_id: UUID) -> Edge | None:
        async with self._engine.connect() as conn:
            return await self._get_by_id(conn, edge_id)

    async def query_by_source(
        self, source_kind: str, source_id: UUID, relation_type: str | None
    ) -> list[Edge]:
        stmt = edges.select().where(
            sa.and_(
                edges.c.source_kind == source_kind,
                edges.c.source_id == source_id,
            )
        )
        if relation_type is not None:
            stmt = stmt.where(edges.c.relation_type == relation_type)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
            return [self._to_domain(row) for row in rows]

    async def query_by_target(
        self, target_kind: str, target_id: UUID, relation_type: str | None
    ) -> list[Edge]:
        stmt = edges.select().where(
            sa.and_(
                edges.c.target_kind == target_kind,
                edges.c.target_id == target_id,
            )
        )
        if relation_type is not None:
            stmt = stmt.where(edges.c.relation_type == relation_type)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
            return [self._to_domain(row) for row in rows]

    async def _get_by_id(self, conn: Any, edge_id: UUID) -> Edge | None:
        row = (
            await conn.execute(edges.select().where(edges.c.id == edge_id))
        ).first()
        return self._to_domain(row) if row else None

    @staticmethod
    def _to_domain(row: Any) -> Edge:
        return Edge(
            id=row.id,
            source_kind=row.source_kind,
            source_id=row.source_id,
            target_kind=row.target_kind,
            target_id=row.target_id,
            relation_type=row.relation_type,
            attributes=row.attributes or {},
            source_passage_id=row.source_passage_id,
            confidence=row.confidence,
            created_at=row.created_at,
        )
