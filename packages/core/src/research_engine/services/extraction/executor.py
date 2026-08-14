"""Extraction executor with caching, semaphore concurrency, and validation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from research_engine.domain.common import ExtractionStatus
from research_engine.domain.errors import LLMError, ValidationError
from research_engine.domain.extractions import (
    ExtractionBatch,
    ExtractionOptions,
    ExtractionResult,
)
from research_engine.services.extraction.schemas import build_output_schema, render_prompt
from research_engine.services.extraction.validation import validate_evidence_spans

if TYPE_CHECKING:
    from uuid import UUID

    from research_engine.ports.llm import LLMPort
    from research_engine.ports.repositories import ExtractionRepo, ExtractionSchemaRepo, PassageRepo

logger = structlog.get_logger()


class ExtractionExecutor:
    def __init__(
        self,
        llm: LLMPort,
        passages: PassageRepo,
        extractions: ExtractionRepo,
        extraction_schemas: ExtractionSchemaRepo,
        default_model: str = "claude-sonnet-4-5-20250929",
    ) -> None:
        self._llm = llm
        self._passages = passages
        self._extractions = extractions
        self._schemas = extraction_schemas
        self._default_model = default_model

    async def execute(
        self,
        passage_ids: list[UUID],
        schema_ref: str,
        options: ExtractionOptions | None = None,
    ) -> ExtractionBatch:
        options = options or ExtractionOptions()

        # Resolve schema
        parts = schema_ref.split(":")
        name = parts[0]
        version = int(parts[1]) if len(parts) > 1 else 1
        schema = await self._schemas.get_by_name_version(name, version)
        if not schema:
            raise ValueError(f"Schema not found: {schema_ref}")

        output_schema = build_output_schema(schema)
        semaphore = asyncio.Semaphore(options.concurrency)

        results: list[ExtractionResult] = []
        for i in range(0, len(passage_ids), options.batch_size):
            batch = passage_ids[i : i + options.batch_size]
            batch_results = await asyncio.gather(*[
                self._extract_one(pid, schema, output_schema, options, semaphore)
                for pid in batch
            ])
            results.extend(batch_results)

        return ExtractionBatch(
            results=results,
            schema_name=name,
            schema_version=version,
        )

    async def _extract_one(
        self,
        passage_id: UUID,
        schema: object,
        output_schema: dict,
        options: ExtractionOptions,
        semaphore: asyncio.Semaphore,
    ) -> ExtractionResult:
        async with semaphore:
            passage = await self._passages.get(passage_id)
            if not passage:
                return ExtractionResult(
                    passage_id=passage_id,
                    status=ExtractionStatus.failed,
                    error="Passage not found",
                )

            # Cache check
            if not options.force_refresh:
                cached = await self._extractions.get_by_key(
                    passage_id, schema.id, options.llm_model or self._default_model
                )
                if cached:
                    return ExtractionResult.from_cached(cached)

            # Render prompt
            model = options.llm_model or self._default_model
            prompt = render_prompt(
                template=schema.prompt_template,
                passage_text=passage.text,
            )

            try:
                llm_response, llm_call_id = await self._llm.structured(
                    messages=[{"role": "user", "content": prompt}],
                    schema=output_schema,
                    model=model,
                    caller=options.caller,
                    purpose="extraction",
                )
            except LLMError as e:
                logger.warning(
                    "extraction_llm_error", passage_id=str(passage_id), error=str(e)
                )
                return ExtractionResult(
                    passage_id=passage_id,
                    status=ExtractionStatus.failed,
                    error=str(e),
                )

            records = llm_response.get("records", [])

            # Validate evidence spans
            try:
                validate_evidence_spans(records, passage.text, passage_id)
            except ValidationError as e:
                if not options.retry_on_validation_error:
                    return ExtractionResult(
                        passage_id=passage_id,
                        status=ExtractionStatus.failed,
                        error=str(e),
                    )
                # One retry with error feedback
                retry_prompt = (
                    f"{prompt}\n\nPrevious attempt failed validation: {e}\n"
                    "Please correct the evidence spans to be exact substrings of the passage."
                )
                try:
                    llm_response, llm_call_id = await self._llm.structured(
                        messages=[{"role": "user", "content": retry_prompt}],
                        schema=output_schema,
                        model=model,
                        caller=options.caller,
                        purpose="extraction_retry",
                    )
                    records = llm_response.get("records", [])
                    validate_evidence_spans(records, passage.text, passage_id)
                except (LLMError, ValidationError) as retry_e:
                    return ExtractionResult(
                        passage_id=passage_id,
                        status=ExtractionStatus.failed,
                        error=str(retry_e),
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
