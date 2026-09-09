"""The span resolver — the single write path for cited addresses.

One row per `(document_id, char_start, char_end)`: every writer of a span goes
through `resolve`, and no writer inserts into `evidence.source_spans`
directly. That is the whole difference between an identity join and
span-overlap geometry — two citers naming the same coordinates share one row,
and a second cite of known coordinates creates a citing row but never a span.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import (
    document_texts,
    passages,
    source_spans,
)
from research_engine.domain.errors import NotFoundError
from research_engine.domain.spans import SourceSpan

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGSourceSpanRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def resolve(
        self,
        tx: Transaction,
        *,
        document_id: UUID,
        char_start: int,
        char_end: int,
    ) -> SourceSpan:
        """Return the span row for these coordinates, creating it if needed.

        On a miss the canonical slice and the document's parser identity are
        read and stored; the caller never supplies text for the row. Runs
        inside the caller's transaction and never commits: two writers racing
        on the same coordinates converge on one row through `ON CONFLICT DO
        NOTHING` plus a re-select.
        """
        if char_start < 0 or char_end <= char_start:
            raise ValueError(
                f"Span [{char_start}, {char_end}) is not an address: "
                "char_start must be non-negative and char_end after it."
            )
        found = await self._get_by_coordinates(
            tx.conn, document_id, char_start, char_end
        )
        if found is not None:
            return found

        slice_text = await self._canonical_slice(tx, document_id, char_start, char_end)
        parser, parser_version = await self._parser_identity(tx, document_id)
        passage_id = await self._best_overlap(tx, document_id, char_start, char_end)
        values = {
            "id": uuid7(),
            "document_id": document_id,
            "char_start": char_start,
            "char_end": char_end,
            "quoted_text": slice_text,
            "parser": parser,
            "parser_version": parser_version,
            "passage_id": passage_id,
        }
        stmt = pg_insert(source_spans).values(**values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[
                source_spans.c.document_id,
                source_spans.c.char_start,
                source_spans.c.char_end,
            ]
        )
        await tx.conn.execute(stmt)
        resolved = await self._get_by_coordinates(
            tx.conn, document_id, char_start, char_end
        )
        assert resolved is not None  # the insert just wrote it, or lost the race to it
        return resolved

    async def get(self, span_id: UUID) -> SourceSpan | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    source_spans.select().where(source_spans.c.id == span_id)
                )
            ).first()
            return self._to_domain(row) if row else None

    async def for_document(self, document_id: UUID) -> list[SourceSpan]:
        """Every span of a document, in address order."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    source_spans.select()
                    .where(source_spans.c.document_id == document_id)
                    .order_by(source_spans.c.char_start, source_spans.c.char_end)
                )
            ).all()
        return [self._to_domain(row) for row in rows]

    async def stale(self, limit: int = 100) -> list[SourceSpan]:
        """Spans whose parser moved under them.

        Staleness is a query, not a column: a span is stale when its
        `parser_version` is distinct from its document's current one, which
        means the offsets may have moved with the re-parse and every citing
        row needs re-checking.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    source_spans.select()
                    .join(
                        document_texts,
                        document_texts.c.document_id == source_spans.c.document_id,
                    )
                    .where(
                        source_spans.c.parser_version.is_distinct_from(
                            document_texts.c.parser_version
                        )
                    )
                    .order_by(source_spans.c.created_at)
                    .limit(limit)
                )
            ).all()
        return [self._to_domain(row) for row in rows]

    async def _get_by_coordinates(
        self, conn: Any, document_id: UUID, char_start: int, char_end: int
    ) -> SourceSpan | None:
        row = (
            await conn.execute(
                source_spans.select().where(
                    sa.and_(
                        source_spans.c.document_id == document_id,
                        source_spans.c.char_start == char_start,
                        source_spans.c.char_end == char_end,
                    )
                )
            )
        ).first()
        return self._to_domain(row) if row else None

    async def _canonical_slice(
        self, tx: Transaction, document_id: UUID, char_start: int, char_end: int
    ) -> str:
        """The stored slice — byte for byte, regardless of the caller's quote."""
        length = char_end - char_start
        text = (
            await tx.conn.execute(
                sa.select(
                    sa.func.substring(document_texts.c.text, char_start + 1, length)
                ).where(document_texts.c.document_id == document_id)
            )
        ).scalar_one_or_none()
        if text is None:
            raise NotFoundError("document_text", document_id)
        if len(text) != length:
            raise ValueError(
                f"Span [{char_start}, {char_end}) runs past the stored text of "
                f"document {document_id}, which holds {char_start + len(text)} "
                "characters here."
            )
        return text

    async def _parser_identity(
        self, tx: Transaction, document_id: UUID
    ) -> tuple[str | None, str | None]:
        row = (
            await tx.conn.execute(
                sa.select(
                    document_texts.c.parser, document_texts.c.parser_version
                ).where(document_texts.c.document_id == document_id)
            )
        ).first()
        if row is None:
            raise NotFoundError("document_text", document_id)
        return row[0], row[1]

    async def _best_overlap(
        self, tx: Transaction, document_id: UUID, char_start: int, char_end: int
    ) -> UUID | None:
        """The passage covering most of the span, newest chunker wins ties.

        The program doc's recovery query, run at write time and cached on the
        row: covering passages for the document, widest overlap first. Rows
        from chunkers that recorded no offsets compare NULL and never match,
        which is what keeps them out without a special case.
        """
        overlap = sa.func.least(passages.c.char_end, char_end) - sa.func.greatest(
            passages.c.char_start, char_start
        )
        return (
            await tx.conn.execute(
                sa.select(passages.c.id)
                .where(
                    passages.c.document_id == document_id,
                    passages.c.char_start < char_end,
                    passages.c.char_end > char_start,
                )
                .order_by(overlap.desc(), passages.c.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    def _to_domain(row: Any) -> SourceSpan:
        return SourceSpan(
            id=row.id,
            document_id=row.document_id,
            char_start=row.char_start,
            char_end=row.char_end,
            quoted_text=row.quoted_text,
            parser=row.parser,
            parser_version=row.parser_version,
            passage_id=row.passage_id,
            created_at=row.created_at,
        )
