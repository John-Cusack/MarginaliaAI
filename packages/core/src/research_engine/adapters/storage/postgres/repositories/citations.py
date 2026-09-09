"""Citation occurrences and items — markers with their groundings.

Reads return occurrences with their items attached (`BlockCitations`), because
no caller ever wants a marker without asking what grounds it next. Writes go
occurrence-then-items in the caller's transaction; a second grounding of one
marker is another item row, never an edit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.repositories.authored import require_draft
from research_engine.adapters.storage.postgres.schema import (
    citation_items,
    citation_occurrences,
    work_blocks,
)
from research_engine.domain.citations import (
    BlockCitations,
    CitationItem,
    CitationItemDraft,
    CitationOccurrence,
    OccurrenceDraft,
)
from research_engine.domain.errors import NotFoundError

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGCitationRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert_occurrence(
        self, tx: Transaction, draft: OccurrenceDraft
    ) -> CitationOccurrence:
        revision_id = await self._revision_of_block(tx, draft.block_id)
        await require_draft(tx.conn, revision_id)
        row = (
            await tx.conn.execute(
                citation_occurrences.insert()
                .values(
                    id=uuid7(),
                    block_id=draft.block_id,
                    citation_key=draft.citation_key,
                    placement=draft.placement.value,
                    intent=draft.intent.value,
                    note=draft.note,
                )
                .returning(citation_occurrences)
            )
        ).first()
        assert row is not None
        return self._occurrence(row)

    async def insert_item(self, tx: Transaction, draft: CitationItemDraft) -> CitationItem:
        revision_id = (
            await tx.conn.execute(
                sa.select(work_blocks.c.revision_id)
                .select_from(
                    work_blocks.join(
                        citation_occurrences,
                        citation_occurrences.c.block_id == work_blocks.c.id,
                    )
                )
                .where(citation_occurrences.c.id == draft.occurrence_id)
            )
        ).scalar_one_or_none()
        if revision_id is None:
            raise NotFoundError("citation_occurrence", draft.occurrence_id)
        await require_draft(tx.conn, revision_id)
        row = (
            await tx.conn.execute(
                citation_items.insert()
                .values(
                    occurrence_id=draft.occurrence_id,
                    position=draft.position,
                    edition_id=draft.edition_id,
                    edition_key=draft.edition_key,
                    source_span_id=draft.source_span_id,
                    quoted_text=draft.quoted_text,
                    verify_status=draft.verify_status,
                    verified_at=sa.func.now()
                    if draft.verify_status
                    else None,
                    locator=draft.locator,
                    prefix=draft.prefix,
                    suffix=draft.suffix,
                    suppress_author=draft.suppress_author,
                )
                .returning(citation_items)
            )
        ).first()
        assert row is not None
        return self._item(row)

    async def for_block(self, block_id: UUID) -> list[BlockCitations]:
        async with self._engine.connect() as conn:
            return await self._attached(
                conn,
                citation_occurrences.select().where(
                    citation_occurrences.c.block_id == block_id
                ),
            )

    async def for_revision(self, revision_id: UUID) -> list[BlockCitations]:
        async with self._engine.connect() as conn:
            return await self._attached(
                conn,
                citation_occurrences.select()
                .select_from(
                    citation_occurrences.join(
                        work_blocks,
                        work_blocks.c.id == citation_occurrences.c.block_id,
                    )
                )
                .where(work_blocks.c.revision_id == revision_id),
            )

    async def by_key(
        self, revision_id: UUID, citation_key: UUID
    ) -> BlockCitations | None:
        async with self._engine.connect() as conn:
            found = await self._attached(
                conn,
                citation_occurrences.select()
                .select_from(
                    citation_occurrences.join(
                        work_blocks,
                        work_blocks.c.id == citation_occurrences.c.block_id,
                    )
                )
                .where(
                    sa.and_(
                        work_blocks.c.revision_id == revision_id,
                        citation_occurrences.c.citation_key == citation_key,
                    )
                ),
            )
            return found[0] if found else None

    async def citing_span(self, span_id: UUID) -> list[BlockCitations]:
        """Every occurrence grounded on this span, for trace and reverify."""
        async with self._engine.connect() as conn:
            return await self._attached(
                conn,
                citation_occurrences.select()
                .select_from(
                    citation_occurrences.join(
                        citation_items,
                        citation_items.c.occurrence_id == citation_occurrences.c.id,
                    )
                )
                .where(citation_items.c.source_span_id == span_id),
            )

    async def citing_key(self, edition_key: str) -> list[BlockCitations]:
        """Every occurrence naming this edition key, for trace."""
        async with self._engine.connect() as conn:
            return await self._attached(
                conn,
                citation_occurrences.select()
                .select_from(
                    citation_occurrences.join(
                        citation_items,
                        citation_items.c.occurrence_id == citation_occurrences.c.id,
                    )
                )
                .where(citation_items.c.edition_key == edition_key),
            )

    async def _attached(self, conn: Any, occurrences: Any) -> list[BlockCitations]:
        rows = (await conn.execute(occurrences)).all()
        out: list[BlockCitations] = []
        for row in rows:
            items = (
                await conn.execute(
                    citation_items.select()
                    .where(citation_items.c.occurrence_id == row.id)
                    .order_by(citation_items.c.position)
                )
            ).all()
            out.append(
                BlockCitations(
                    occurrence=self._occurrence(row),
                    items=[self._item(item) for item in items],
                )
            )
        return out

    async def _revision_of_block(self, tx: Transaction, block_id: UUID) -> UUID:
        revision_id = (
            await tx.conn.execute(
                sa.select(work_blocks.c.revision_id).where(
                    work_blocks.c.id == block_id
                )
            )
        ).scalar_one_or_none()
        if revision_id is None:
            raise NotFoundError("work_block", block_id)
        return revision_id

    @staticmethod
    def _occurrence(row: Any) -> CitationOccurrence:
        return CitationOccurrence(
            id=row.id,
            citation_key=row.citation_key,
            block_id=row.block_id,
            placement=row.placement,
            intent=row.intent,
            note=row.note,
            created_at=row.created_at,
        )

    @staticmethod
    def _item(row: Any) -> CitationItem:
        return CitationItem(
            occurrence_id=row.occurrence_id,
            position=row.position,
            edition_id=row.edition_id,
            edition_key=row.edition_key,
            source_span_id=row.source_span_id,
            quoted_text=row.quoted_text,
            verify_status=row.verify_status,
            verified_at=row.verified_at,
            locator=dict(row.locator or {}),
            prefix=row.prefix,
            suffix=row.suffix,
            suppress_author=row.suppress_author,
        )
