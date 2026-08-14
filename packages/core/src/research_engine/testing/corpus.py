"""A scratch corpus that removes exactly what it created."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import documents, passages

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def new_id() -> UUID:
    """A uuid7 as a stdlib UUID.

    ``uuid_utils.UUID`` does not compare equal to the ``uuid.UUID`` asyncpg
    returns, so fixtures hand out the type the database will hand back.
    """
    return UUID(str(uuid7()))


class Corpus:
    """Creates documents and passages, and deletes them again.

    Isolation is by deletion of tracked rows, never by truncating ``core.*``.
    Passages, FTS rows and embeddings go with their document via ON DELETE
    CASCADE, so tracking document ids covers most of it; anything that outlives
    a document — entities, extraction schemas — must be registered with
    :meth:`track`.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._document_ids: list[UUID] = []
        self._extra: list[tuple[Any, UUID]] = []

    def track(self, table: Any, row_id: UUID) -> UUID:
        """Register a row that does not cascade with a document.

        Entities and extraction schemas outlive the documents referencing them,
        so a test creating one must say so or it leaks — and
        ``extraction_schemas`` has a unique key, so the leak makes the *next*
        run fail rather than this one.
        """
        self._extra.append((table, row_id))
        return row_id

    async def add_document(
        self,
        *,
        language: str | None = None,
        document_type: str = "test_doc",
        title: str = "test",
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        doc_id = new_id()
        async with self._engine.begin() as conn:
            await conn.execute(
                documents.insert().values(
                    id=doc_id,
                    title=title,
                    document_type=document_type,
                    language=language,
                    source=source or f"test://{doc_id}",
                    content_hash=doc_id.bytes,
                    parser="test",
                    parser_version="1.0",
                    metadata=metadata or {},
                )
            )
        self._document_ids.append(doc_id)
        return doc_id

    async def add_passage(
        self,
        document_id: UUID,
        text: str,
        *,
        position: int = 0,
        char_start: int = 0,
        char_end: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        pid = new_id()
        async with self._engine.begin() as conn:
            await conn.execute(
                passages.insert().values(
                    id=pid,
                    document_id=document_id,
                    position=position,
                    char_start=char_start,
                    char_end=char_start + len(text) if char_end is None else char_end,
                    locator={},
                    text=text,
                    token_count=len(text.split()),
                    chunker="test",
                    chunker_version="1.0",
                    metadata=metadata or {},
                    content_hash=pid.bytes,
                )
            )
        return pid

    def adopt(self, document_id: UUID) -> UUID:
        """Track a document created by something else — an ingest under test.

        Without this, a test that exercises the real ingestion path leaves its
        documents behind. That is precisely how the corpus acquired books it was
        never asked to hold.
        """
        self._document_ids.append(document_id)
        return document_id

    async def cleanup(self) -> None:
        # Documents first: their cascades clear the rows referencing the tracked
        # entities and schemas, which would otherwise block deletion.
        async with self._engine.begin() as conn:
            if self._document_ids:
                await conn.execute(
                    documents.delete().where(documents.c.id.in_(self._document_ids))
                )
            for table, row_id in reversed(self._extra):
                await conn.execute(table.delete().where(table.c.id == row_id))
        self._document_ids.clear()
        self._extra.clear()


async def count_rows(engine: AsyncEngine, table: Any) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(sa.select(sa.func.count()).select_from(table))
        ).scalar_one()
