"""Passage and search domain types."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from research_engine.domain.common import FusionMode


class Passage(BaseModel):
    """A chunk of a document — the unit of retrieval."""

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
    content_hash: bytes
    created_at: datetime


class PassageDraft(BaseModel):
    """Data needed to create a passage record.

    ``char_start`` / ``char_end`` are the passage's span in the document's
    canonical text, and are required: they are the address every other feature
    hangs off — pin-cites, quote verification, annotations, re-chunking. The
    contract every chunker must satisfy is::

        draft.text == canonical_text[draft.char_start:draft.char_end]

    ``locator`` stays for type-specific extras (page, verse, timecode) that are
    meaningful to a reader but not usable as an address.
    """

    position: int
    char_start: int
    char_end: int
    locator: dict[str, Any] = Field(default_factory=dict)
    text: str
    token_count: int | None = None
    chunker: str
    chunker_version: str
    metadata: dict[str, Any] = Field(default_factory=dict)

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


class PassageHit(BaseModel):
    """A passage returned by search with scores."""

    passage_id: UUID
    document_id: UUID
    score: float
    score_breakdown: ScoreBreakdown | None = None
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    locator: dict[str, Any] = Field(default_factory=dict)
    context_available: bool = True


class ScoreBreakdown(BaseModel):
    """Breakdown of how the score was computed."""

    vector: float | None = None
    keyword: float | None = None
    rerank: float | None = None
    rrf: float | None = None


class SearchQuery(BaseModel):
    """Search query with filters and hybrid options."""

    text: str
    filters: SearchFilters | None = None
    k: int = 20
    k_vec: int = 100
    k_kw: int = 100
    fusion_mode: FusionMode = FusionMode.rrf
    alpha: float = 0.5
    rerank: bool = True
    rerank_n: int = 30


class SearchFilters(BaseModel):
    """Filters for narrowing search results."""

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


class SearchResult(BaseModel):
    """Result of a search operation."""

    hits: list[PassageHit]
    total_candidates: int
    applied_filters: dict[str, Any] = Field(default_factory=dict)
