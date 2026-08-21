"""Run an extraction schema over passages, and keep what it produced.

The engine's first principle is that anything an LLM concludes lands in a table
with its confidence and its source passage, so it is inspectable, correctable,
and citable. That makes persistence the feature, not a side effect: an executor
that returns records to its caller and writes nothing has produced an opinion,
not evidence. This one writes an ``extractions`` row per passage — successes and
failures alike, because a failed run is also a finding — and materializes each
record into ``extraction_records`` with the character offsets of the passage
text it quoted.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from uuid_utils import uuid7

from research_engine.domain.common import ExtractionStatus
from research_engine.domain.errors import (
    LLMError,
    LLMUnavailable,
    ValidationError,
)
from research_engine.domain.extractions import (
    Extraction,
    ExtractionBatch,
    ExtractionOptions,
    ExtractionRecord,
    ExtractionResult,
)
from research_engine.services.extraction.caching import extractor_version
from research_engine.services.extraction.schemas import (
    build_output_schema,
    record_type_definitions,
    render_prompt,
)
from research_engine.services.extraction.validation import (
    ValidatedRecord,
    validate_records,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from research_engine.domain.extractions import ExtractionSchema
    from research_engine.ports.llm import LLMPort
    from research_engine.ports.repositories import (
        ExtractionRepo,
        ExtractionSchemaRepo,
        PassageRepo,
    )
    from research_engine.services.extraction.postprocess import RecordEnricher

logger = structlog.get_logger()


class ExtractionExecutor:
    def __init__(
        self,
        llm: LLMPort,
        passages: PassageRepo,
        extractions: ExtractionRepo,
        extraction_schemas: ExtractionSchemaRepo,
        transaction_factory: Any,
        default_model: str = "claude-sonnet-4-5-20250929",
        enricher: RecordEnricher | None = None,
    ) -> None:
        self._llm = llm
        self._passages = passages
        self._extractions = extractions
        self._schemas = extraction_schemas
        self._transaction = transaction_factory
        self._default_model = default_model
        #: Resolves `fuzzy_date` and `entity_ref` fields after validation. The
        #: composition root always supplies one; without it those field types
        #: are stored as the strings the model wrote, which is what they were
        #: before this existed.
        self._enricher = enricher

    async def execute(
        self,
        passage_ids: list[UUID],
        schema_ref: str,
        options: ExtractionOptions | None = None,
    ) -> ExtractionBatch:
        options = options or ExtractionOptions()

        parts = schema_ref.split(":")
        name = parts[0]
        version = int(parts[1]) if len(parts) > 1 else 1
        schema = await self._schemas.get_by_name_version(name, version)
        if not schema:
            raise ValueError(f"Schema not found: {schema_ref}")

        output_schema = build_output_schema(schema)
        record_types = record_type_definitions(schema)
        model = options.llm_model or self._default_model
        version_key = extractor_version(schema.version, schema.prompt_template, model)
        semaphore = asyncio.Semaphore(options.concurrency)

        results: list[ExtractionResult] = []
        for i in range(0, len(passage_ids), options.batch_size):
            batch = passage_ids[i : i + options.batch_size]
            results.extend(
                await asyncio.gather(*[
                    self._extract_one(
                        pid,
                        schema,
                        output_schema,
                        record_types,
                        options,
                        semaphore,
                        model,
                        version_key,
                    )
                    for pid in batch
                ])
            )

        return ExtractionBatch(
            results=results, schema_name=name, schema_version=version
        )

    async def _extract_one(
        self,
        passage_id: UUID,
        schema: ExtractionSchema,
        output_schema: dict,
        record_types: Mapping[str, dict],
        options: ExtractionOptions,
        semaphore: asyncio.Semaphore,
        model: str,
        version_key: str,
    ) -> ExtractionResult:
        async with semaphore:
            passage = await self._passages.get(passage_id)
            if not passage:
                # Nothing to write: `extractions.passage_id` is a foreign key,
                # so there is no row this failure could hang from.
                return ExtractionResult(
                    passage_id=passage_id,
                    status=ExtractionStatus.failed,
                    error="Passage not found",
                )

            if not options.force_refresh:
                cached = await self._extractions.get_by_key(
                    passage_id, schema.id, version_key
                )
                # A cached failure is a record of what happened, not an answer.
                # Serving it back would make a transient provider outage
                # permanent for every passage it touched.
                if cached and cached.status == ExtractionStatus.ok:
                    return ExtractionResult.from_cached(cached)

            prompt = render_prompt(
                template=schema.prompt_template, passage_text=passage.text
            )
            try:
                validated, llm_call_id = await self._ask(
                    prompt, output_schema, record_types, passage, model, options
                )
            except LLMUnavailable:
                # Not this passage's failure — no passage can succeed. Marking
                # each one and continuing would write a failed row for every
                # passage in the corpus and bury the one fact that matters.
                logger.error(
                    "extraction_abandoned",
                    schema=schema.name,
                    reason="the LLM is unavailable",
                )
                raise
            except (LLMError, ValidationError) as exc:
                logger.warning(
                    "extraction_failed",
                    passage_id=str(passage_id),
                    schema=schema.name,
                    error=str(exc),
                )
                await self._store(
                    passage_id, schema, version_key, model, ExtractionStatus.failed,
                    records=[], validated=[], llm_call_id=None, error=str(exc),
                )
                return ExtractionResult(
                    passage_id=passage_id,
                    status=ExtractionStatus.failed,
                    error=str(exc),
                )

            if self._enricher is not None:
                validated = await self._enricher.enrich(
                    validated, passage, record_types
                )
            records = [_serialize(record) for record in validated]
            await self._store(
                passage_id, schema, version_key, model, ExtractionStatus.ok,
                records=records, validated=validated, llm_call_id=llm_call_id,
                error=None,
            )
            logger.info(
                "extraction_complete",
                passage_id=str(passage_id),
                schema=schema.name,
                records_count=len(records),
            )
            return ExtractionResult(
                passage_id=passage_id,
                status=ExtractionStatus.ok,
                records=records,
                from_cache=False,
                llm_call_id=llm_call_id,
            )

    async def _ask(
        self,
        prompt: str,
        output_schema: dict,
        record_types: Mapping[str, dict],
        passage: Any,
        model: str,
        options: ExtractionOptions,
    ) -> tuple[list[ValidatedRecord], UUID]:
        """One call, then at most one corrective retry carrying the error text."""
        response, call_id = await self._llm.structured(
            messages=[{"role": "user", "content": prompt}],
            schema=output_schema,
            model=model,
            caller=options.caller,
            purpose="extraction",
        )
        try:
            return (
                validate_records(
                    response.get("records", []),
                    passage.text,
                    passage.id,
                    record_types,
                ),
                call_id,
            )
        except ValidationError as exc:
            if not options.retry_on_validation_error:
                raise
            # Bound to a name here because Python unbinds the exception
            # variable on leaving the block, and the retry needs the text.
            rejection = str(exc)

        retry_prompt = (
            f"{prompt}\n\nYour previous answer was rejected: {rejection}\n"
            f"Return the same records with that corrected. Quote the passage "
            f"word for word in every evidence field."
        )
        response, call_id = await self._llm.structured(
            messages=[{"role": "user", "content": retry_prompt}],
            schema=output_schema,
            model=model,
            caller=options.caller,
            purpose="extraction_retry",
        )
        return (
            validate_records(
                response.get("records", []), passage.text, passage.id, record_types
            ),
            call_id,
        )

    async def _store(
        self,
        passage_id: UUID,
        schema: ExtractionSchema,
        version_key: str,
        model: str,
        status: ExtractionStatus,
        *,
        records: list[dict],
        validated: list[ValidatedRecord],
        llm_call_id: UUID | None,
        error: str | None,
    ) -> None:
        """Write the extraction and its records as one unit.

        Materialized records exist to be queried; the ``records`` JSON on the
        extraction itself is what the model actually said. Splitting the write
        would let one survive without the other, and there would be no way
        afterwards to tell which of the two to believe.
        """
        extraction = Extraction(
            id=_new_id(),
            passage_id=passage_id,
            schema_id=schema.id,
            extractor_version=version_key,
            llm_model=model,
            status=status,
            error=error,
            records=records,
            llm_call_id=llm_call_id,
            created_at=_NOW,
        )
        async with self._transaction() as tx:
            stored = await self._extractions.save(tx, extraction)
            await self._extractions.replace_records(
                tx,
                stored.id,
                [
                    ExtractionRecord(
                        id=_new_id(),
                        extraction_id=stored.id,
                        passage_id=passage_id,
                        schema_id=schema.id,
                        record_type=record.record_type,
                        data=record.fields,
                        evidence_start=record.evidence_start,
                        evidence_end=record.evidence_end,
                        created_at=_NOW,
                    )
                    for record in validated
                ],
            )


def _new_id() -> UUID:
    """A uuid7 as a stdlib UUID.

    `uuid_utils.uuid7()` returns its own UUID class. Repositories pass it
    straight to the driver, which accepts it; a pydantic domain model does not,
    and rejects it as "UUID input should be a string, bytes or UUID object".
    """
    return UUID(str(uuid7()))


def _serialize(record: ValidatedRecord) -> dict[str, Any]:
    return {
        "record_type": record.record_type,
        "fields": record.fields,
        "evidence_start": record.evidence_start,
        "evidence_end": record.evidence_end,
    }


#: `created_at` is server-generated; the domain model requires a value, and this
#: placeholder never reaches a column. The row read back after the write carries
#: the real timestamp.
_NOW = datetime.fromtimestamp(0, UTC)
