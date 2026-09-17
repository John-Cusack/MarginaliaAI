"""Cited addresses — spans of canonical document text.

A span owns its address and its canonical slice. The citing row (an anchor, a
citation item) owns the typed quote, the tier, and the timestamp. That split
is decision 1, and it is what lets two citers share one span while disagreeing
about its wording.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

from pydantic import BaseModel


class SourceSpan(BaseModel):
    """One row of `evidence.source_spans`."""

    id: UUID
    document_id: UUID
    char_start: int
    char_end: int
    #: `document_texts.text[char_start:char_end]`, written by the resolver.
    #: Never the citer's typed quote.
    quoted_text: str
    parser: str | None = None
    parser_version: str | None = None
    #: Best-overlap passage, a cache only. Null when no passage overlaps.
    passage_id: UUID | None = None
    created_at: datetime
