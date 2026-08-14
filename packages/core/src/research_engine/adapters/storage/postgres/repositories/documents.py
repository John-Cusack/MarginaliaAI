"""Postgres document repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import documents
from research_engine.domain.documents import Document, DocumentDraft, DocumentFilter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGDocumentRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, tx: Transaction, draft: DocumentDraft) -> Document:
        doc_id = uuid7()
        values = {
            "id": doc_id,
            "title": draft.title,
            "document_type": draft.document_type,
            "language": draft.language,
            "source": draft.source,
            "content_hash": draft.content_hash,
            "parser": draft.parser,
            "parser_version": draft.parser_version,
            "created_date_start": draft.created_date_start,
            "created_date_end": draft.created_date_end,
            "created_precision": draft.created_precision,
            "metadata": draft.metadata,
        }
        await tx.conn.execute(documents.insert().values(**values))
        return await self._get_by_id(tx.conn, doc_id)

    async def get(self, doc_id: UUID) -> Document | None:
        async with self._engine.connect() as conn:
            return await self._get_by_id(conn, doc_id)

    async def _get_by_id(self, conn: Any, doc_id: UUID) -> Document | None:
        row = (await conn.execute(documents.select().where(documents.c.id == doc_id))).first()
        return self._to_domain(row) if row else None

    async def find_by_hash(self, content_hash: bytes, source: str) -> Document | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    documents.select().where(
                        sa.and_(
                            documents.c.content_hash == content_hash,
                            documents.c.source == source,
                        )
                    )
                )
            ).first()
            return self._to_domain(row) if row else None

    async def update_metadata(self, doc_id: UUID, patch: dict[str, Any]) -> Document:
        async with self._engine.begin() as conn:
            # Merge metadata
            existing = (
                await conn.execute(
                    sa.select(documents.c.metadata).where(documents.c.id == doc_id)
                )
            ).scalar_one()
            merged = {**(existing or {}), **patch}
            await conn.execute(
                documents.update().where(documents.c.id == doc_id).values(metadata=merged)
            )
            return await self._get_by_id(conn, doc_id)  # type: ignore[return-value]

    async def iter_by_filter(self, filter: DocumentFilter) -> AsyncIterator[Document]:
        stmt = documents.select()
        stmt = self._apply_filter(stmt, filter)
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            for row in result:
                yield self._to_domain(row)

    async def count(self, filter: DocumentFilter | None = None) -> int:
        stmt = sa.select(sa.func.count()).select_from(documents)
        if filter:
            stmt = self._apply_filter(stmt, filter)
        async with self._engine.connect() as conn:
            return (await conn.execute(stmt)).scalar_one()

    async def delete(self, doc_id: UUID) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(documents.delete().where(documents.c.id == doc_id))

    def _apply_filter(self, stmt: Any, f: DocumentFilter) -> Any:
        if f.document_types:
            stmt = stmt.where(documents.c.document_type.in_(f.document_types))
        if f.date_start:
            stmt = stmt.where(documents.c.created_date_start >= f.date_start)
        if f.date_end:
            stmt = stmt.where(documents.c.created_date_end <= f.date_end)
        if f.language:
            stmt = stmt.where(documents.c.language == f.language)
        if f.source_pattern:
            stmt = stmt.where(documents.c.source.ilike(f"%%{f.source_pattern}%%"))
        return stmt

    @staticmethod
    def _to_domain(row: Any) -> Document:
        return Document(
            id=row.id,
            title=row.title,
            document_type=row.document_type,
            language=row.language,
            source=row.source,
            content_hash=bytes(row.content_hash),
            parser=row.parser,
            parser_version=row.parser_version,
            ingested_at=row.ingested_at,
            created_date_start=row.created_date_start,
            created_date_end=row.created_date_end,
            created_precision=row.created_precision,
            metadata=row.metadata or {},
        )
