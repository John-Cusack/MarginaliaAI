"""Postgres extraction and extraction schema repositories."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import (
    extraction_records,
    extraction_schemas,
    extractions,
)
from research_engine.domain.extractions import (
    Extraction,
    ExtractionRecord,
    ExtractionSchema,
    ExtractionSchemaDraft,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGExtractionSchemaRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, tx: Transaction, draft: ExtractionSchemaDraft) -> ExtractionSchema:
        schema_id = uuid7()
        values = {
            "id": schema_id,
            "name": draft.name,
            "version": draft.version,
            "owner": draft.owner,
            "schema": draft.schema_def,
            "prompt_template": draft.prompt_template,
        }
        await tx.conn.execute(extraction_schemas.insert().values(**values))
        return await self._get_by_id(tx.conn, schema_id)  # type: ignore[return-value]

    async def get(self, schema_id: UUID) -> ExtractionSchema | None:
        async with self._engine.connect() as conn:
            return await self._get_by_id(conn, schema_id)

    async def get_by_name_version(
        self, name: str, version: int
    ) -> ExtractionSchema | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    extraction_schemas.select().where(
                        sa.and_(
                            extraction_schemas.c.name == name,
                            extraction_schemas.c.version == version,
                        )
                    )
                )
            ).first()
            return self._to_domain(row) if row else None

    async def list_all(self) -> list[ExtractionSchema]:
        async with self._engine.connect() as conn:
            rows = (await conn.execute(extraction_schemas.select())).all()
            return [self._to_domain(row) for row in rows]

    async def _get_by_id(self, conn: Any, schema_id: UUID) -> ExtractionSchema | None:
        row = (
            await conn.execute(
                extraction_schemas.select().where(extraction_schemas.c.id == schema_id)
            )
        ).first()
        return self._to_domain(row) if row else None

    @staticmethod
    def _to_domain(row: Any) -> ExtractionSchema:
        return ExtractionSchema(
            id=row.id,
            name=row.name,
            version=row.version,
            owner=row.owner,
            schema=row.schema,
            prompt_template=row.prompt_template,
            created_at=row.created_at,
        )


class PGExtractionRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, tx: Transaction, extraction: Extraction) -> Extraction:
        values = {
            "id": extraction.id,
            "passage_id": extraction.passage_id,
            "schema_id": extraction.schema_id,
            "extractor_version": extraction.extractor_version,
            "llm_model": extraction.llm_model,
            "status": extraction.status.value,
            "error": extraction.error,
            "records": extraction.records,
            "llm_call_id": extraction.llm_call_id,
        }
        await tx.conn.execute(extractions.insert().values(**values))
        return await self._get_by_id(tx.conn, extraction.id)  # type: ignore[return-value]

    async def get_by_key(
        self, passage_id: UUID, schema_id: UUID, extractor_version: str
    ) -> Extraction | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    extractions.select().where(
                        sa.and_(
                            extractions.c.passage_id == passage_id,
                            extractions.c.schema_id == schema_id,
                            extractions.c.extractor_version == extractor_version,
                        )
                    )
                )
            ).first()
            return self._to_domain(row) if row else None

    async def insert_records(
        self, tx: Transaction, records: list[ExtractionRecord]
    ) -> None:
        for record in records:
            values = {
                "id": record.id,
                "extraction_id": record.extraction_id,
                "passage_id": record.passage_id,
                "schema_id": record.schema_id,
                "record_type": record.record_type,
                "data": record.data,
                "evidence_start": record.evidence_start,
                "evidence_end": record.evidence_end,
            }
            await tx.conn.execute(extraction_records.insert().values(**values))

    async def query_records(
        self, record_type: str, filters: dict[str, Any] | None, k: int
    ) -> list[ExtractionRecord]:
        stmt = (
            extraction_records.select()
            .where(extraction_records.c.record_type == record_type)
        )
        if filters:
            stmt = stmt.where(
                extraction_records.c.data.op("@>")(sa.type_coerce(filters, sa.JSON))
            )
        stmt = stmt.limit(k)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
            return [self._record_to_domain(row) for row in rows]

    async def _get_by_id(self, conn: Any, extraction_id: UUID) -> Extraction | None:
        row = (
            await conn.execute(
                extractions.select().where(extractions.c.id == extraction_id)
            )
        ).first()
        return self._to_domain(row) if row else None

    @staticmethod
    def _to_domain(row: Any) -> Extraction:
        return Extraction(
            id=row.id,
            passage_id=row.passage_id,
            schema_id=row.schema_id,
            extractor_version=row.extractor_version,
            llm_model=row.llm_model,
            status=row.status,
            error=row.error,
            records=row.records or [],
            llm_call_id=row.llm_call_id,
            created_at=row.created_at,
        )

    @staticmethod
    def _record_to_domain(row: Any) -> ExtractionRecord:
        return ExtractionRecord(
            id=row.id,
            extraction_id=row.extraction_id,
            passage_id=row.passage_id,
            schema_id=row.schema_id,
            record_type=row.record_type,
            data=row.data,
            evidence_start=row.evidence_start,
            evidence_end=row.evidence_end,
            created_at=row.created_at,
        )
