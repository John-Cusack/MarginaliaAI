"""Document marks — ranges over canonical text that the node tree cannot hold."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from research_engine.adapters.storage.postgres.schema import document_marks
from research_engine.domain.acquisitions import DocumentMark

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.domain.acquisitions import DocumentMarkDraft
    from research_engine.ports.repositories import Transaction


class PGDocumentMarkRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert_many(
        self, tx: Transaction, document_id: UUID, drafts: list[DocumentMarkDraft]
    ) -> int:
        if not drafts:
            return 0
        await tx.conn.execute(
            document_marks.insert(),
            [
                {
                    "document_id": document_id,
                    "kind": d.kind,
                    "char_start": d.char_start,
                    "char_end": d.char_end,
                    "attrs": d.attrs,
                }
                for d in drafts
            ],
        )
        return len(drafts)

    async def for_document(
        self, document_id: UUID, *, kind: str | None = None
    ) -> list[DocumentMark]:
        query = (
            document_marks.select()
            .where(document_marks.c.document_id == document_id)
            .order_by(document_marks.c.kind, document_marks.c.char_start)
        )
        if kind is not None:
            query = query.where(document_marks.c.kind == kind)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [_to_domain(row) for row in rows]

    async def overlapping(
        self, document_id: UUID, char_start: int, char_end: int, *, kind: str
    ) -> list[DocumentMark]:
        """Marks of one kind touching a span — the pages a quotation falls on.

        A zero-width mark (a page break) touches a span it sits inside or at
        either edge of.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    document_marks.select()
                    .where(
                        document_marks.c.document_id == document_id,
                        document_marks.c.kind == kind,
                        document_marks.c.char_start <= char_end,
                        document_marks.c.char_end >= char_start,
                    )
                    .order_by(document_marks.c.char_start)
                )
            ).all()
        return [_to_domain(row) for row in rows]

    async def count(self, document_id: UUID, *, kind: str | None = None) -> int:
        query = sa.select(sa.func.count()).where(
            document_marks.c.document_id == document_id
        )
        if kind is not None:
            query = query.where(document_marks.c.kind == kind)
        async with self._engine.connect() as conn:
            return (await conn.execute(query)).scalar_one()


def _to_domain(row: Any) -> DocumentMark:
    return DocumentMark(
        id=row.id,
        document_id=row.document_id,
        kind=row.kind,
        char_start=row.char_start,
        char_end=row.char_end,
        attrs=dict(row.attrs or {}),
    )
