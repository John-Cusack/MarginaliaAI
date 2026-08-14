"""Extraction framework domain types."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from research_engine.domain.common import ExtractionStatus


class ExtractionSchema(BaseModel):
    """A registered extraction schema."""

    id: UUID
    name: str
    version: int
    owner: str
    schema_def: dict[str, Any] = Field(alias="schema")
    prompt_template: str
    created_at: datetime

    model_config = {"populate_by_name": True}


class ExtractionSchemaDraft(BaseModel):
    """Data needed to register an extraction schema."""

    name: str
    version: int
    owner: str
    schema_def: dict[str, Any]
    prompt_template: str


class Extraction(BaseModel):
    """One row per (passage x schema x extractor_version) invocation."""

    id: UUID
    passage_id: UUID
    schema_id: UUID
    extractor_version: str
    llm_model: str
    status: ExtractionStatus
    error: str | None = None
    records: list[dict[str, Any]] = Field(default_factory=list)
    llm_call_id: UUID | None = None
    created_at: datetime


class ExtractionRecord(BaseModel):
    """A single extracted record, materialized for queryability."""

    id: UUID
    extraction_id: UUID
    passage_id: UUID
    schema_id: UUID
    record_type: str
    data: dict[str, Any]
    evidence_start: int | None = None
    evidence_end: int | None = None
    created_at: datetime


class ExtractionOptions(BaseModel):
    """Options for running an extraction."""

    force_refresh: bool = False
    llm_model: str | None = None
    concurrency: int = 8
    batch_size: int = 10
    retry_on_validation_error: bool = True
    caller: str = "core"


class ExtractionResult(BaseModel):
    """Result of extracting from a single passage."""

    passage_id: UUID
    status: ExtractionStatus
    records: list[dict[str, Any]] = Field(default_factory=list)
    from_cache: bool = False
    llm_call_id: UUID | None = None
    error: str | None = None

    @classmethod
    def from_cached(cls, extraction: Extraction) -> ExtractionResult:
        return cls(
            passage_id=extraction.passage_id,
            status=extraction.status,
            records=extraction.records,
            from_cache=True,
            llm_call_id=extraction.llm_call_id,
        )


class ExtractionBatch(BaseModel):
    """Result of a batch extraction."""

    results: list[ExtractionResult]
    schema_name: str
    schema_version: int
