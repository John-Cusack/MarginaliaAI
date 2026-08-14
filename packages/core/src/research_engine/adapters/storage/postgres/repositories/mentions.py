"""Postgres mention repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import mentions
from research_engine.domain.entities import Mention, MentionDraft

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGMentionRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, tx: Transaction, draft: MentionDraft) -> Mention:
        mention_id = uuid7()
        values = {
            "id": mention_id,
            "passage_id": draft.passage_id,
            "entity_id": draft.entity_id,
            "span_start": draft.span_start,
            "span_end": draft.span_end,
            "surface_form": draft.surface_form,
            "confidence": draft.confidence,
            "source": draft.source.value,
        }
        await tx.conn.execute(mentions.insert().values(**values))
        return await self._get_by_id(tx.conn, mention_id)  # type: ignore[return-value]

    async def insert_many(self, tx: Transaction, drafts: list[MentionDraft]) -> list[Mention]:
        ids: list[UUID] = []
        for draft in drafts:
            mention_id = uuid7()
            ids.append(mention_id)
            values = {
                "id": mention_id,
                "passage_id": draft.passage_id,
                "entity_id": draft.entity_id,
                "span_start": draft.span_start,
                "span_end": draft.span_end,
                "surface_form": draft.surface_form,
                "confidence": draft.confidence,
                "source": draft.source.value,
            }
            await tx.conn.execute(mentions.insert().values(**values))
        rows = (
            await tx.conn.execute(mentions.select().where(mentions.c.id.in_(ids)))
        ).all()
        return [self._to_domain(row) for row in rows]

    async def get_by_passage(self, passage_id: UUID) -> list[Mention]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    mentions.select().where(mentions.c.passage_id == passage_id)
                )
            ).all()
            return [self._to_domain(row) for row in rows]

    async def get_by_entity(
        self, entity_id: UUID, filters: dict[str, Any] | None, k: int
    ) -> list[Mention]:
        stmt = mentions.select().where(mentions.c.entity_id == entity_id)
        if filters:
            if "source" in filters:
                stmt = stmt.where(mentions.c.source == filters["source"])
            if "min_confidence" in filters:
                stmt = stmt.where(mentions.c.confidence >= filters["min_confidence"])
        stmt = stmt.limit(k)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
            return [self._to_domain(row) for row in rows]

    async def _get_by_id(self, conn: Any, mention_id: UUID) -> Mention | None:
        row = (
            await conn.execute(mentions.select().where(mentions.c.id == mention_id))
        ).first()
        return self._to_domain(row) if row else None

    @staticmethod
    def _to_domain(row: Any) -> Mention:
        return Mention(
            id=row.id,
            passage_id=row.passage_id,
            entity_id=row.entity_id,
            span_start=row.span_start,
            span_end=row.span_end,
            surface_form=row.surface_form,
            confidence=row.confidence,
            source=row.source,
            created_at=row.created_at,
        )
