"""Canonical document text — the substrate passage offsets address."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from research_engine.adapters.storage.postgres.schema import document_texts
from research_engine.domain.documents import DocumentText
from research_engine.services.text.normalize import NORMALIZATION_VERSION, normalize

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGDocumentTextRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def put(
        self,
        tx: Transaction,
        document_id: UUID,
        text: str,
        parser: str,
        parser_version: str,
    ) -> None:
        """Store (or replace) a document's canonical text.

        Upsert rather than insert: re-parsing a document under a new parser
        version replaces the substrate, and that is a re-anchoring event the
        caller is expected to follow with a re-chunk.
        """
        values = {
            "document_id": document_id,
            "text": text,
            "normalized_text": normalize(text),
            "normalization_version": NORMALIZATION_VERSION,
            "parser": parser,
            "parser_version": parser_version,
        }
        stmt = pg_insert(document_texts).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[document_texts.c.document_id],
            set_={k: v for k, v in values.items() if k != "document_id"},
        )
        await tx.conn.execute(stmt)

    async def get(self, document_id: UUID) -> DocumentText | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    document_texts.select().where(
                        document_texts.c.document_id == document_id
                    )
                )
            ).first()
        if row is None:
            return None
        return DocumentText(
            document_id=row.document_id,
            text=row.text,
            normalized_text=row.normalized_text,
            normalization_version=row.normalization_version,
            parser=row.parser,
            parser_version=row.parser_version,
        )

    async def get_text(self, document_id: UUID) -> str | None:
        """Just the raw text, for callers that do not need the rest."""
        async with self._engine.connect() as conn:
            return (
                await conn.execute(
                    sa.select(document_texts.c.text).where(
                        document_texts.c.document_id == document_id
                    )
                )
            ).scalar_one_or_none()

    async def get_span(
        self, document_id: UUID, start: int, end: int
    ) -> str | None:
        """One slice of a document's canonical text, sliced by the database.

        `get_text` then `text[start:end]` reads the whole document to return a
        fragment of it. That was harmless while a document was a batch of a
        hundred articles; a merged reference work is twenty-five megabytes, and
        reading all of it to answer `read_node` on a single lexicon entry is the
        difference between a query and a stall. `substring` does the slice where
        the text already lives.

        Returns None when the document has no stored text — the same answer as
        `get_text`, so callers distinguish "no text" from "empty slice".
        """
        # Clamped rather than returned early: an empty span on a document that
        # has no text must still answer None, and Postgres rejects a negative
        # substring length outright.
        length = max(end - start, 0)
        async with self._engine.connect() as conn:
            return (
                await conn.execute(
                    sa.select(
                        # SQL substring is 1-indexed and takes a length, not an
                        # end offset; Python's slice is 0-indexed and half-open.
                        sa.func.substring(
                            document_texts.c.text, start + 1, length
                        )
                    ).where(document_texts.c.document_id == document_id)
                )
            ).scalar_one_or_none()

    async def missing_document_ids(self, limit: int | None = None) -> list[UUID]:
        """Documents with no canonical text stored.

        These were ingested before `document_texts` existed; nothing can be
        re-anchored for them until the text is reconstructed.
        """
        from research_engine.adapters.storage.postgres.schema import documents

        stmt = (
            sa.select(documents.c.id)
            .outerjoin(
                document_texts, document_texts.c.document_id == documents.c.id
            )
            .where(document_texts.c.document_id.is_(None))
            .order_by(documents.c.ingested_at)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._engine.connect() as conn:
            return [row[0] for row in await conn.execute(stmt)]

    async def count(self) -> int:
        async with self._engine.connect() as conn:
            return (
                await conn.execute(
                    sa.select(sa.func.count()).select_from(document_texts)
                )
            ).scalar_one()
