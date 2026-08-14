"""SourceSearchProvider — pluggable cross-source discovery for ingestion.

Plugins register a provider that maps a structured ``SourceQuery`` to a list
of ``SourceMatch`` records the agent can act on. The core ``search_sources``
MCP tool fans out across all registered providers in parallel, deduplicates
across sources, and enriches matches with corpus-presence info so the agent
can build an ingestion plan from a reading list in one round trip.
"""

from __future__ import annotations

import enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Availability(enum.StrEnum):
    """Whether and how the user can ingest this source.

    Ordering is intentional: dedup keeps the higher-availability match.
    """

    in_corpus = "in_corpus"            # already ingested — short-circuit
    ingestable = "ingestable"          # owned/free, can ingest now
    borrowable = "borrowable"          # available via library loan
    purchasable = "purchasable"        # requires purchase
    external_only = "external_only"    # metadata-only (e.g. paywalled abstract)


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
    """Structured query for cross-source discovery.

    ``query`` is the free-text fallback. The other fields are optional hints
    that providers may use for higher-precision matching when caller has them
    (e.g. parsed from a citation).
    """

    query: str
    title: str | None = None
    author: str | None = None
    year: int | None = None
    doi: str | None = None
    isbn: str | None = None
    asin: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class IngestAction(BaseModel):
    """Side-effect descriptor: how to ingest this match.

    Kept structured (tool + args) rather than calling ingestion directly so
    ``search_sources`` stays read-only. The agent (or a follow-up tool) is
    free to invoke it.
    """

    tool: str                          # e.g. "logos_ingest_book", "acad_discover_by_doi"
    args: dict[str, Any] = Field(default_factory=dict)


class SourceMatch(BaseModel):
    """One result from a SourceSearchProvider."""

    plugin: str                        # provider name, e.g. "acad", "logos"
    source_id: str                     # plugin-native id (resource_id, doi, book_id…)
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None             # first-class identity for cross-source dedup
    isbn: str | None = None            # first-class identity for cross-source dedup
    availability: Availability = Availability.external_only
    confidence: float = 0.0            # 0..1, plugin-scored
    ingest_action: IngestAction | None = None
    document_id: str | None = None     # populated by core when availability=in_corpus
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class SourceSearchProvider(Protocol):
    """Each plugin that owns a source implements this Protocol.

    Registered in ``pack.yaml`` under ``provides.source_search``.
    Core composes registered providers into the ``search_sources`` MCP tool.
    """

    @property
    def plugin_name(self) -> str:
        """Stable identifier, e.g. 'acad', 'logos', 'ycl', 'kindle'."""
        ...

    async def search(self, query: SourceQuery, *, limit: int) -> list[SourceMatch]:
        """Map *query* to matches. Must not raise on transient errors —
        return [] and log instead, so a single slow/broken source can't
        poison the fan-out.
        """
        ...
