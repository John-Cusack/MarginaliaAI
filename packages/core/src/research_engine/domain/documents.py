"""Document domain types."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class Document(BaseModel):
    """A fully ingested document."""

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


class DocumentDraft(BaseModel):
    """Data needed to create a document record."""

    title: str | None = None
    document_type: str = "generic"
    language: str | None = None
    source: str
    content_hash: bytes
    parser: str
    parser_version: str
    created_date_start: datetime | None = None
    created_date_end: datetime | None = None
    created_precision: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentText(BaseModel):
    """A document's canonical text — the substrate passage offsets address.

    ``text`` is authoritative: passage ``char_start`` / ``char_end`` index into
    it. ``normalized_text`` is a lossy fold for quote matching and must never be
    used for addressing.
    """

    document_id: UUID
    text: str
    normalized_text: str
    normalization_version: str
    parser: str
    parser_version: str


class DocumentFilter(BaseModel):
    """Filters for querying documents."""

    document_types: list[str] | None = None
    date_start: datetime | None = None
    date_end: datetime | None = None
    language: str | None = None
    source_pattern: str | None = None
    metadata: dict[str, Any] | None = None
