"""Citation occurrences and items — where a block leans on the corpus.

An occurrence is the `{{cite:<key>}}` marker's row: which block, which intent,
which placement. An item is one grounding of that marker: which span, which
typed quote at which tier, which identity. Two items over one span keep their
own wording and tier — decision 1, the same split the ledger's anchors use.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from research_engine.domain.works import Placement
from research_engine.domain.works_files import Intent


class CitationOccurrence(BaseModel):
    id: UUID
    citation_key: UUID
    block_id: UUID
    placement: Placement = Placement.INLINE
    intent: Intent = Intent.SOURCE
    note: str | None = None
    created_at: datetime


class OccurrenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    citation_key: UUID
    placement: Placement = Placement.INLINE
    intent: Intent = Intent.SOURCE
    note: str | None = None


class CitationItem(BaseModel):
    occurrence_id: UUID
    position: int
    edition_id: UUID | None = None
    zotero_key: str | None = None
    source_span_id: UUID | None = None
    quoted_text: str | None = None
    verify_status: str | None = None
    verified_at: datetime | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    prefix: str | None = None
    suffix: str | None = None
    suppress_author: bool = False


class CitationItemDraft(BaseModel):
    """An item validates before the database does.

    Identity first: an item with neither `edition_id` nor `zotero_key` names
    no edition and is refused here, not by the check constraint. Then the
    quote: typed wording without a span is a claim without an address.
    """

    model_config = ConfigDict(extra="forbid")

    occurrence_id: UUID
    position: int = 0
    edition_id: UUID | None = None
    zotero_key: str | None = None
    source_span_id: UUID | None = None
    quoted_text: str | None = None
    verify_status: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    prefix: str | None = None
    suffix: str | None = None
    suppress_author: bool = False

    @model_validator(mode="after")
    def _names_an_edition(self) -> CitationItemDraft:
        if self.edition_id is None and self.zotero_key is None:
            raise ValueError("an item names its edition: edition_id or zotero_key")
        return self

    @model_validator(mode="after")
    def _quote_needs_a_span(self) -> CitationItemDraft:
        if self.quoted_text is not None and self.source_span_id is None:
            raise ValueError("quoted_text without source_span_id is unaddressed")
        return self


class BlockCitations(BaseModel):
    """One occurrence with everything grounding it, for export and validate."""

    occurrence: CitationOccurrence
    items: list[CitationItem] = Field(default_factory=list)
