"""A scratch corpus that removes exactly what it created."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.authored import (
    PGWorkRepo,
    PGWorkRevisionRepo,
)
from research_engine.adapters.storage.postgres.repositories.spans import PGSourceSpanRepo
from research_engine.adapters.storage.postgres.schema import (
    documents,
    passages,
    source_spans,
    works,
)
from research_engine.domain.works import WorkDraft, WorkRevisionDraft

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.domain.spans import SourceSpan
    from research_engine.domain.works import Work, WorkRevision


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
        self._span_ids: list[UUID] = []
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

    async def add_span(
        self, document_id: UUID, char_start: int, char_end: int
    ) -> SourceSpan:
        """Resolve a span through the real resolver and track it for cleanup."""
        async with transaction(self._engine) as tx:
            span = await PGSourceSpanRepo(self._engine).resolve(
                tx,
                document_id=document_id,
                char_start=char_start,
                char_end=char_end,
            )
        self._span_ids.append(span.id)
        return span

    def adopt_span(self, span_id: UUID) -> UUID:
        """Track a span resolved by something else — a citer under test.

        A span RESTRICT-guards its document, so an untracked span fails
        document cleanup with a foreign key violation rather than a leak.
        """
        self._span_ids.append(span_id)
        return span_id

    async def add_work(
        self, slug: str, title: str = "A work", work_type: str = "essay"
    ) -> tuple[Work, WorkRevision]:
        """Create a work with its first draft revision, tracked for cleanup.

        The work row cascades to revisions, blocks, occurrences, items, links
        and waivers, so tracking the work alone removes the whole tree —
        after spans and documents, which RESTRICT from below.
        """
        async with transaction(self._engine) as tx:
            work = await PGWorkRepo(self._engine).insert(
                tx, WorkDraft(slug=slug, title=title, work_type=work_type)
            )
            revision = await PGWorkRevisionRepo(self._engine).insert(
                tx, WorkRevisionDraft(work_id=work.id, revision_number=1)
            )
            await PGWorkRepo(self._engine).set_current_revision(
                tx, work.id, revision.id
            )
        self._extra.append((works, work.id))
        return work, revision

    async def cleanup(self) -> None:
        # Works first: they cascade to items and links, which RESTRICT the
        # spans and editions below. Spans next: they RESTRICT their document.
        # Documents next: their cascades clear the rows referencing tracked
        # entities and schemas. The remaining extras (entities, editions, …)
        # go last, unreferenced by then.
        first = [(table, row_id) for table, row_id in self._extra if table is works]
        rest = [
            (table, row_id) for table, row_id in self._extra if table is not works
        ]
        async with self._engine.begin() as conn:
            for table, row_id in reversed(first):
                await conn.execute(table.delete().where(table.c.id == row_id))
            if self._span_ids:
                await conn.execute(
                    source_spans.delete().where(source_spans.c.id.in_(self._span_ids))
                )
            if self._document_ids:
                await conn.execute(
                    documents.delete().where(documents.c.id.in_(self._document_ids))
                )
            for table, row_id in reversed(rest):
                await conn.execute(table.delete().where(table.c.id == row_id))
        self._span_ids.clear()
        self._document_ids.clear()
        self._extra.clear()


async def count_rows(engine: AsyncEngine, table: Any) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(sa.select(sa.func.count()).select_from(table))
        ).scalar_one()
