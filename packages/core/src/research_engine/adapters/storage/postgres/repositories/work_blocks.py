"""Work blocks — the ordered, keyed units of a revision.

A block's identity is its `block_key`, stable across revisions while its row
id turns over on every copy-forward. Positions are unique per parent within a
revision (roots group under NULL), and a parent from another revision is
rejected by the composite foreign key before any application code runs.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.repositories.authored import require_draft
from research_engine.adapters.storage.postgres.schema import work_blocks
from research_engine.domain.errors import NotFoundError, StaleWriteError
from research_engine.domain.works import WorkBlock, WorkBlockDraft

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGWorkBlockRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert(
        self,
        tx: Transaction,
        revision_id: UUID,
        draft: WorkBlockDraft,
        *,
        expected_updated_at: Any,
    ) -> WorkBlock:
        """Insert by key, or update under optimistic locking.

        Updates require `expected_updated_at` and refuse on mismatch — two
        writers on one block resolve by re-reading, never by last-write-wins.
        """
        await require_draft(tx.conn, revision_id)
        existing = await self.by_key_in_tx(tx, revision_id, draft.block_key)
        if existing is None:
            row = (
                await tx.conn.execute(
                    work_blocks.insert()
                    .values(
                        id=uuid7(),
                        revision_id=revision_id,
                        block_key=draft.block_key,
                        parent_id=draft.parent_id,
                        position=draft.position,
                        block_type=draft.block_type,
                        title=draft.title,
                        body_markdown=draft.body_markdown,
                        attributes=draft.attributes,
                    )
                    .returning(work_blocks)
                )
            ).first()
            assert row is not None
            return self._to_domain(row)
        if not _same_updated_at(existing.updated_at, expected_updated_at):
            raise StaleWriteError(
                f"Block {existing.id} changed under you: re-read and retry."
            )
        row = (
            await tx.conn.execute(
                work_blocks.update()
                .where(work_blocks.c.id == existing.id)
                .values(
                    parent_id=draft.parent_id,
                    position=draft.position,
                    block_type=draft.block_type,
                    title=draft.title,
                    body_markdown=draft.body_markdown,
                    attributes=draft.attributes,
                    updated_at=sa.func.now(),
                )
                .returning(work_blocks)
            )
        ).first()
        assert row is not None
        return self._to_domain(row)

    async def tree(self, revision_id: UUID) -> list[WorkBlock]:
        """Every block of a revision, depth-first: parents before children."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    work_blocks.select()
                    .where(work_blocks.c.revision_id == revision_id)
                    .order_by(work_blocks.c.position)
                )
            ).all()
        blocks = [self._to_domain(row) for row in rows]
        children: dict[Any, list[WorkBlock]] = {}
        for block in blocks:
            children.setdefault(str(block.parent_id), []).append(block)
        ordered: list[WorkBlock] = []

        def visit(parent: Any) -> None:
            for child in children.get(str(parent), []):
                ordered.append(child)
                visit(child.id)

        visit(None)
        return ordered

    async def by_key(self, revision_id: UUID, block_key: UUID) -> WorkBlock | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    work_blocks.select().where(
                        sa.and_(
                            work_blocks.c.revision_id == revision_id,
                            work_blocks.c.block_key == block_key,
                        )
                    )
                )
            ).first()
            return self._to_domain(row) if row else None

    async def by_key_in_tx(
        self, tx: Transaction, revision_id: UUID, block_key: UUID
    ) -> WorkBlock | None:
        row = (
            await tx.conn.execute(
                work_blocks.select().where(
                    sa.and_(
                        work_blocks.c.revision_id == revision_id,
                        work_blocks.c.block_key == block_key,
                    )
                )
            )
        ).first()
        return self._to_domain(row) if row else None

    async def get(self, block_id: UUID) -> WorkBlock | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    work_blocks.select().where(work_blocks.c.id == block_id)
                )
            ).first()
            return self._to_domain(row) if row else None

    async def delete(self, tx: Transaction, block_id: UUID) -> None:
        """Delete a block; children RESTRICT, occurrences and links cascade.

        A block with children refuses (the composite FK), so deletion walks
        leaves first. Occurrences, items, and links go with their block.
        """
        row = (
            await tx.conn.execute(
                sa.select(work_blocks.c.revision_id).where(
                    work_blocks.c.id == block_id
                )
            )
        ).first()
        if row is None:
            raise NotFoundError("work_block", block_id)
        await require_draft(tx.conn, row[0])
        await tx.conn.execute(work_blocks.delete().where(work_blocks.c.id == block_id))

    @staticmethod
    def _to_domain(row: Any) -> WorkBlock:
        return WorkBlock(
            id=row.id,
            revision_id=row.revision_id,
            block_key=row.block_key,
            parent_id=row.parent_id,
            position=row.position,
            block_type=row.block_type,
            title=row.title,
            body_markdown=row.body_markdown,
            attributes=dict(row.attributes or {}),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _same_updated_at(actual: datetime, expected: Any) -> bool:
    if isinstance(expected, str):
        try:
            expected = datetime.fromisoformat(expected.replace("Z", "+00:00"))
        except ValueError:
            return False
    return actual == expected
