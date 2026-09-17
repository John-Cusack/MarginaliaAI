"""Block links — a block's relationships to spans and entities.

Source links say what a block leans on (quotes, translates, discusses);
entity links say what it renders (the lemma query's `renders` relation).
Both cascade with their block and RESTRICT what they name: deleting a span or
entity under a live link refuses, deleting the block takes its links along.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from research_engine.adapters.storage.postgres.repositories.authored import require_draft
from research_engine.adapters.storage.postgres.schema import (
    block_entity_links,
    block_source_links,
    work_blocks,
)
from research_engine.domain.errors import NotFoundError
from research_engine.domain.works import (
    BlockEntityLink,
    BlockEntityLinkDraft,
    BlockLinks,
    BlockSourceLink,
    BlockSourceLinkDraft,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGWorkLinkRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_source_link(
        self, tx: Transaction, draft: BlockSourceLinkDraft
    ) -> BlockSourceLink:
        await self._require_block_draft(tx, draft.block_id)
        row = (
            await tx.conn.execute(
                block_source_links.insert()
                .values(
                    block_id=draft.block_id,
                    source_span_id=draft.source_span_id,
                    relation=draft.relation,
                    confidence=draft.confidence,
                    note=draft.note,
                )
                .returning(block_source_links)
            )
        ).first()
        assert row is not None
        return self._source(row)

    async def add_entity_link(
        self, tx: Transaction, draft: BlockEntityLinkDraft
    ) -> BlockEntityLink:
        await self._require_block_draft(tx, draft.block_id)
        row = (
            await tx.conn.execute(
                block_entity_links.insert()
                .values(
                    block_id=draft.block_id,
                    entity_id=draft.entity_id,
                    relation=draft.relation,
                    surface_form=draft.surface_form,
                )
                .returning(block_entity_links)
            )
        ).first()
        assert row is not None
        return self._entity(row)

    async def for_block(self, block_id: UUID) -> BlockLinks:
        async with self._engine.connect() as conn:
            sources = (
                await conn.execute(
                    block_source_links.select().where(
                        block_source_links.c.block_id == block_id
                    )
                )
            ).all()
            entities = (
                await conn.execute(
                    block_entity_links.select().where(
                        block_entity_links.c.block_id == block_id
                    )
                )
            ).all()
        return BlockLinks(
            sources=[self._source(row) for row in sources],
            entities=[self._entity(row) for row in entities],
        )

    async def for_span(self, span_id: UUID) -> list[BlockSourceLink]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    block_source_links.select().where(
                        block_source_links.c.source_span_id == span_id
                    )
                )
            ).all()
        return [self._source(row) for row in rows]

    async def for_entity(
        self, entity_id: UUID, relation: str
    ) -> list[BlockEntityLink]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    block_entity_links.select().where(
                        sa.and_(
                            block_entity_links.c.entity_id == entity_id,
                            block_entity_links.c.relation == relation,
                        )
                    )
                )
            ).all()
        return [self._entity(row) for row in rows]

    async def _require_block_draft(self, tx: Transaction, block_id: UUID) -> None:
        revision_id = (
            await tx.conn.execute(
                sa.select(work_blocks.c.revision_id).where(
                    work_blocks.c.id == block_id
                )
            )
        ).scalar_one_or_none()
        if revision_id is None:
            raise NotFoundError("work_block", block_id)
        await require_draft(tx.conn, revision_id)

    @staticmethod
    def _source(row: Any) -> BlockSourceLink:
        return BlockSourceLink(
            block_id=row.block_id,
            source_span_id=row.source_span_id,
            relation=row.relation,
            confidence=row.confidence,
            note=row.note,
            created_at=row.created_at,
        )

    @staticmethod
    def _entity(row: Any) -> BlockEntityLink:
        return BlockEntityLink(
            block_id=row.block_id,
            entity_id=row.entity_id,
            relation=row.relation,
            surface_form=row.surface_form,
            created_at=row.created_at,
        )
