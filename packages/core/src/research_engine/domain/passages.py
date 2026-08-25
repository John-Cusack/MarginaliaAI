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
    #: The structural node containing this passage, when the document has a
    #: tree. None for corpora ingested before nodes existed.
    node_id: UUID | None = None
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
    #: Resolved after the document tree is written, since node ids do not
    #: exist while a chunker is running.
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


class PassageWindow(BaseModel):
    """The expanded read of a hit — more than matched, bounded by structure.

    Not what was ranked. The chunk is, and it is still on the hit as ``text``.
    This is what a person or an agent should *read*: a chunk boundary is where
    the ingester happened to cut, which in a lexicon lands mid-definition and in
    a monograph mid-argument.

    Keeps the same invariant ``PassageDraft`` keeps —
    ``text == canonical_text[char_start:char_end]`` — so a window can be quoted
    and re-located exactly like a passage can.
    """

    text: str
    char_start: int
    char_end: int
    #: How the boundaries were chosen. ``node`` means the window *is* one
    #: structural node and `read_node` would add nothing; anything else means it
    #: is a slice and reading the node would give more.
    source: Literal["node", "node_window", "document_window", "passage"]
    #: The node that bounded the window — not always the passage's own node. A
    #: node narrower than the chunk gets widened inside its parent instead.
    node_id: UUID | None = None
    #: Ancestor titles, root first. The citation for this window.
    breadcrumb: list[str] = Field(default_factory=list)
    #: Measured on the returned text, not on the estimate that sized it.
    approx_tokens: int


class PassageHit(BaseModel):
    """A passage returned by search with scores."""

    passage_id: UUID
    document_id: UUID
    score: float
    score_breakdown: ScoreBreakdown | None = None
    #: The chunk that actually matched — what was embedded, ranked and reranked.
    #: Quote this. Read ``window``.
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    locator: dict[str, Any] = Field(default_factory=dict)
    #: The chunk's span in the document's canonical text. Carried so a caller can
    #: widen a hit without re-reading the row; ``None`` on passages written
    #: before offsets were required.
    char_start: int | None = None
    char_end: int | None = None
    node_id: UUID | None = None
    #: ``None`` when the document has no canonical text to widen into, or the
    #: passage has no offsets to widen from.
    window: PassageWindow | None = None


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
    #: Stages that were skipped because a backend was unreachable, e.g.
    #: ``["rerank_unavailable"]``. Empty means the search ran in full.
    #:
    #: This exists so that degrading is never silent. Reranking is 99.4% of
    #: query latency, so when the GPU host is down the only sensible thing is to
    #: return fused results without it — but results ordered by RRF alone are
    #: measurably different from reranked ones, and a researcher comparing
    #: today's hits against last week's deserves to know which they are looking
    #: at rather than inferring it from a mood.
    degraded: list[str] = Field(default_factory=list)
