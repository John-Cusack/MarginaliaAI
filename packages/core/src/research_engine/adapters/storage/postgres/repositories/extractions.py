"""Postgres extraction and extraction schema repositories."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
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

    async def save(self, tx: Transaction, draft: ExtractionSchemaDraft) -> ExtractionSchema:
        """Register a schema, updating it in place if that version exists.

        Editing the prompt of a schema under development is the common case, and
        it must not require bumping the version — the extraction cache keys on a
        digest of the prompt, so an edit already invalidates the right rows
        without renaming the schema out from under the extractions that cite it.
        """
        values = {
            "id": uuid7(),
            "name": draft.name,
            "version": draft.version,
            "owner": draft.owner,
            "schema": draft.schema_def,
            "prompt_template": draft.prompt_template,
        }
        stmt = pg_insert(extraction_schemas).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                extraction_schemas.c.name,
                extraction_schemas.c.version,
                extraction_schemas.c.owner,
            ],
            set_={
                "schema": stmt.excluded.schema,
                "prompt_template": stmt.excluded.prompt_template,
            },
        ).returning(extraction_schemas.c.id)
        schema_id = (await tx.conn.execute(stmt)).scalar_one()
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

    async def save(self, tx: Transaction, extraction: Extraction) -> Extraction:
        """Write an extraction, replacing any earlier run of the same key.

        ``(passage_id, schema_id, extractor_version)`` is unique, and re-running
        a schema over a passage is the normal case — a prompt is tuned, a
        provider outage is retried. A plain insert turns that into an integrity
        error, so this upserts and returns the row that now exists, whose id the
        caller needs to hang materialized records from.
        """
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
        stmt = pg_insert(extractions).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                extractions.c.passage_id,
                extractions.c.schema_id,
                extractions.c.extractor_version,
            ],
            set_={
                "llm_model": stmt.excluded.llm_model,
                "status": stmt.excluded.status,
                "error": stmt.excluded.error,
                "records": stmt.excluded.records,
                "llm_call_id": stmt.excluded.llm_call_id,
                "created_at": sa.func.now(),
            },
        ).returning(extractions.c.id)
        stored_id = (await tx.conn.execute(stmt)).scalar_one()
        return await self._get_by_id(tx.conn, stored_id)  # type: ignore[return-value]

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

    async def replace_records(
        self, tx: Transaction, extraction_id: UUID, records: list[ExtractionRecord]
    ) -> None:
        """Make the materialized records match this run exactly.

        Deleting first is what keeps the two representations honest: a re-run
        that finds three claims where the last found five must not leave the
        other two queryable, still citing an answer no longer given.
        """
        await tx.conn.execute(
            extraction_records.delete().where(
                extraction_records.c.extraction_id == extraction_id
            )
        )
        if not records:
            return
        await tx.conn.execute(
            extraction_records.insert(),
            [
                {
                    "id": record.id,
                    "extraction_id": record.extraction_id,
                    "passage_id": record.passage_id,
                    "schema_id": record.schema_id,
                    "record_type": record.record_type,
                    "data": record.data,
                    "evidence_start": record.evidence_start,
                    "evidence_end": record.evidence_end,
                }
                for record in records
            ],
        )

    async def get_record(self, record_id: UUID) -> ExtractionRecord | None:
        """One materialized record, by its own id.

        `provenance_of` reached for this through `query_records` with an empty
        record type and a `record_id` key that is a column, not a data field —
        so tracing an extraction record's provenance matched nothing, whatever
        was stored.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    extraction_records.select().where(
                        extraction_records.c.id == record_id
                    )
                )
            ).first()
            return self._record_to_domain(row) if row else None

    async def query_records(
        self,
        record_type: str,
        data_filter: dict[str, Any] | None = None,
        passage_ids: list[UUID] | None = None,
        k: int = 100,
    ) -> list[ExtractionRecord]:
        """Records of one type, optionally narrowed by their data or passages.

        ``data`` is a ``json`` column and ``@>`` is a ``jsonb`` operator, so the
        comparison casts. That means no index assists it — acceptable while this
        table is small, and the reason the filter is a containment test rather
        than an arbitrary expression: it can be answered by a GIN index on a
        ``jsonb`` column the day the volume justifies migrating to one.
        """
        stmt = extraction_records.select().where(
            extraction_records.c.record_type == record_type
        )
        if data_filter:
            stmt = stmt.where(
                sa.cast(extraction_records.c.data, JSONB).contains(
                    sa.cast(sa.literal(data_filter, sa.JSON), JSONB)
                )
            )
        if passage_ids is not None:
            if not passage_ids:
                return []
            stmt = stmt.where(extraction_records.c.passage_id.in_(passage_ids))
        stmt = stmt.order_by(extraction_records.c.id).limit(k)
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
