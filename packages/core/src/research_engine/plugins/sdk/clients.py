"""Scoped service clients injected at load time for plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from uuid import UUID

    from research_engine.domain.passages import SearchQuery, SearchResult


class CorpusClient(Protocol):
    """Read-only access to corpus. Always granted.

    Implemented by ``adapters.corpus_client.CorpusServiceAdapter``. The simple
    ``find_passages(query, filters=, k=)`` surface covers the common case;
    ``find_passages_advanced`` is the escape hatch for plugins that need
    fusion mode, rerank, or k_vec/k_kw control.
    """

    async def find_passages(
        self, query: str, filters: dict[str, Any] | None = None, k: int = 20
    ) -> SearchResult: ...
    async def find_passages_advanced(self, query: SearchQuery) -> SearchResult: ...
    async def get_document(self, document_id: UUID) -> dict[str, Any] | None: ...
    async def get_passage_context(
        self, passage_id: UUID, before: int = 0, after: int = 0
    ) -> dict[str, Any]: ...


class ExtractionClient(Protocol):
    """Run extractions or query cached extraction records."""

    async def extract(
        self, passage_ids: list[UUID], schema: str, options: dict | None = None
    ) -> dict: ...
    async def query_records(
        self, record_type: str, filters: dict | None = None, k: int = 100
    ) -> list: ...


class EdgeClient(Protocol):
    """Create/read typed graph edges — requires permissions.write = True.

    Implemented by ``adapters.edge_client.EdgeServiceAdapter``. ``create`` takes
    a flat dict (``source_kind``/``source_id``/``target_kind``/``target_id``/
    ``relation_type`` plus optional ``attributes``/``source_passage_id``/
    ``confidence``) and dedups on the natural key, so re-creating an edge
    enriches rather than duplicates.
    """

    async def create(self, edge: dict) -> dict: ...
    async def query(
        self,
        *,
        source_id: UUID | None = None,
        target_id: UUID | None = None,
        relation_type: str | None = None,
    ) -> list[dict]: ...


class LLMClient(Protocol):
    """Direct LLM invocation — requires permissions.llm = True."""

    async def complete(self, messages: list[dict], model: str | None = None, **opts: Any) -> str: ...
    async def structured(
        self, messages: list[dict], schema: dict, model: str | None = None
    ) -> dict: ...


class HttpClient(Protocol):
    """HTTP fetches — requires permissions.network != none."""

    async def get(self, url: str) -> bytes: ...
    async def post(self, url: str, json: Any = None) -> bytes: ...


class EntityClient(Protocol):
    """Create/update entities."""

    async def upsert(self, entity: dict) -> dict: ...
    async def resolve(self, name: str, entity_type: str | None = None) -> list: ...


class EventClient(Protocol):
    """Create/query events."""

    async def create(self, event: dict) -> dict: ...
    async def query(self, filters: dict, k: int = 1000) -> list: ...


class IngestionClient(Protocol):
    """Trigger core ingestion pipeline — requires permissions.ingest = True."""

    async def ingest_paths(self, paths: list, hint: str | None = None) -> dict: ...

    async def ingest_drafts(
        self,
        title: str,
        document_type: str,
        passage_drafts: list,
        *,
        source: str = "",
        metadata: dict | None = None,
        language: str | None = None,
        full_text: str | None = None,
    ) -> dict: ...

    async def find_existing(
        self, *, source: str | None = None, source_pattern: str | None = None
    ) -> list[dict]: ...
