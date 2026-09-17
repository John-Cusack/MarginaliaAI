"""Validated data transfer objects crossing the plugin boundary."""

from __future__ import annotations

import enum
from datetime import datetime  # noqa: TC003 - Pydantic resolves this at runtime
from pathlib import Path  # noqa: TC003 - Pydantic resolves this at runtime
from typing import Any, Literal
from uuid import UUID  # noqa: TC003 - Pydantic resolves this at runtime

from pydantic import BaseModel, Field, model_validator


class DatePrecision(enum.StrEnum):
    day = "day"
    week = "week"
    month = "month"
    season = "season"
    year = "year"
    decade = "decade"


class NodeKind(enum.StrEnum):
    entity = "entity"
    document = "document"
    passage = "passage"
    event = "event"


class MentionSource(enum.StrEnum):
    llm_extraction = "llm_extraction"
    rule = "rule"
    manual = "manual"


class FusionMode(enum.StrEnum):
    rrf = "rrf"
    weighted = "weighted"
    vector_only = "vector_only"
    keyword_only = "keyword_only"


class ExtractionStatus(enum.StrEnum):
    pending = "pending"
    ok = "ok"
    failed = "failed"


class SourceRef(BaseModel):
    """Reference to a local source or URI."""

    path: Path | None = None
    uri: str | None = None
    content_hash: bytes | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_one_location(self) -> SourceRef:
        if self.path is None and not self.uri:
            raise ValueError("SourceRef requires path or uri")
        return self

    @property
    def ref(self) -> str:
        return str(self.path) if self.path is not None else (self.uri or "")

    @property
    def is_local(self) -> bool:
        return self.path is not None


class DetectionResult(BaseModel):
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    is_viable: bool = True


class ParsedDocument(BaseModel):
    title: str | None = None
    text: str
    document_type: str = "generic"
    language: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    structural_locators: list[dict[str, Any]] = Field(default_factory=list)


class PassageDraft(BaseModel):
    """A chunk whose span addresses the canonical document text."""

    position: int = Field(ge=0)
    char_start: int
    char_end: int
    locator: dict[str, Any] = Field(default_factory=dict)
    text: str
    token_count: int | None = Field(default=None, ge=0)
    chunker: str
    chunker_version: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    node_id: UUID | None = None

    @model_validator(mode="after")
    def _span_is_well_formed(self) -> PassageDraft:
        if self.char_start < 0:
            raise ValueError(f"char_start must be non-negative, got {self.char_start}")
        if self.char_end < self.char_start:
            raise ValueError(
                f"char_end ({self.char_end}) precedes char_start ({self.char_start})"
            )
        if self.char_end - self.char_start != len(self.text):
            raise ValueError(
                f"span width {self.char_end - self.char_start} does not match "
                f"text length {len(self.text)} — the span and the text disagree"
            )
        return self


class NodeDraft(BaseModel):
    """A document-tree node before storage assigns an id."""

    path: str
    parent_path: str | None
    depth: int = Field(ge=0)
    position: int = Field(ge=0)
    node_type: str = "section"
    title: str | None = None
    char_start: int = Field(ge=0)
    char_end: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _span_is_well_formed(self) -> NodeDraft:
        if self.char_end < self.char_start:
            raise ValueError(
                f"char_end ({self.char_end}) precedes char_start ({self.char_start})"
            )
        return self


class PluginContext(BaseModel):
    plugin_id: str
    data_dir: Path
    distribution_name: str
    distribution_version: str


class FuzzyDate(BaseModel):
    start: datetime
    end: datetime
    precision: DatePrecision


class Document(BaseModel):
    id: UUID
    title: str | None = None
    document_type: str
    language: str | None = None
    source: str
    content_hash: bytes
    parser: str
    parser_version: str
    ingested_at: datetime
    created_date_start: datetime | None = None
    created_date_end: datetime | None = None
    created_precision: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Passage(BaseModel):
    id: UUID
    document_id: UUID
    position: int
    char_start: int | None = None
    char_end: int | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    text: str
    token_count: int | None = None
    chunker: str
    chunker_version: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    node_id: UUID | None = None
    content_hash: bytes
    created_at: datetime


class Entity(BaseModel):
    id: UUID
    entity_type: str
    canonical_name: str
    disambiguator: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class Mention(BaseModel):
    id: UUID
    passage_id: UUID
    entity_id: UUID
    span_start: int | None = None
    span_end: int | None = None
    surface_form: str
    confidence: float
    source: MentionSource
    created_at: datetime


class Event(BaseModel):
    id: UUID
    event_type: str
    timestamp_start: datetime | None = None
    timestamp_end: datetime | None = None
    precision: DatePrecision | None = None
    location_id: UUID | None = None
    location_text: str | None = None
    source_passage_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    created_at: datetime


class EventFilter(BaseModel):
    event_types: list[str] | None = None
    actor_entity_ids: list[UUID] | None = None
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None
    location_id: UUID | None = None
    payload: dict[str, Any] | None = None


class TimelineBucket(BaseModel):
    bucket: str
    count: int
    aggregates: dict[str, Any] = Field(default_factory=dict)


class Edge(BaseModel):
    id: UUID
    source_kind: NodeKind
    source_id: UUID
    target_kind: NodeKind
    target_id: UUID
    relation_type: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_passage_id: UUID | None = None
    confidence: float = 1.0
    created_at: datetime


class ExtractionRecord(BaseModel):
    id: UUID
    extraction_id: UUID
    passage_id: UUID
    schema_id: UUID
    record_type: str
    data: dict[str, Any]
    evidence_start: int | None = None
    evidence_end: int | None = None
    created_at: datetime


class SearchFilters(BaseModel):
    document_types: list[str] | None = None
    date_range_start: str | None = None
    date_range_end: str | None = None
    author_entity_id: UUID | None = None
    recipient_entity_id: UUID | None = None
    mentions_entity_ids: list[UUID] | None = None
    metadata: dict[str, Any] | None = None
    language: str | None = None
    extensions: dict[str, Any] | None = None
    extension_logic: Literal["and", "or"] = "and"


class SearchQuery(BaseModel):
    text: str
    filters: SearchFilters | None = None
    k: int = 20
    k_vec: int = 100
    k_kw: int = 100
    fusion_mode: FusionMode = FusionMode.rrf
    alpha: float = 0.5
    rerank: bool = True
    rerank_n: int = 30


class ScoreBreakdown(BaseModel):
    vector: float | None = None
    keyword: float | None = None
    rerank: float | None = None
    rrf: float | None = None


class PassageHit(BaseModel):
    passage_id: UUID
    document_id: UUID
    score: float
    score_breakdown: ScoreBreakdown | None = None
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    locator: dict[str, Any] = Field(default_factory=dict)
    char_start: int | None = None
    char_end: int | None = None
    node_id: UUID | None = None


class SearchResult(BaseModel):
    hits: list[PassageHit]
    total_candidates: int
    applied_filters: dict[str, Any] = Field(default_factory=dict)
    degraded: list[str] = Field(default_factory=list)


class Availability(enum.StrEnum):
    in_corpus = "in_corpus"
    ingestable = "ingestable"
    borrowable = "borrowable"
    purchasable = "purchasable"
    external_only = "external_only"


_AVAILABILITY_RANK: dict[Availability, int] = {
    Availability.in_corpus: 4,
    Availability.ingestable: 3,
    Availability.borrowable: 2,
    Availability.purchasable: 1,
    Availability.external_only: 0,
}


def availability_rank(value: Availability) -> int:
    return _AVAILABILITY_RANK[value]


class SourceQuery(BaseModel):
    query: str
    title: str | None = None
    author: str | None = None
    year: int | None = None
    doi: str | None = None
    isbn: str | None = None
    asin: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class IngestAction(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class SourceMatch(BaseModel):
    plugin: str
    source_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    isbn: str | None = None
    availability: Availability = Availability.external_only
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ingest_action: IngestAction | None = None
    document_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
