"""Permission-scoped service protocols injected into plugin handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from research_engine_sdk.types import (
        Event,
        EventFilter,
        NodeDraft,
        PassageDraft,
        SearchQuery,
        SearchResult,
        TimelineBucket,
    )


@runtime_checkable
class CorpusClient(Protocol):
    async def find_passages(
        self, query: str, filters: dict[str, Any] | None = None, k: int = 20
    ) -> SearchResult: ...

    async def find_passages_advanced(self, query: SearchQuery) -> SearchResult: ...

    async def get_document(self, document_id: UUID) -> dict[str, Any] | None: ...

    async def get_document_outline(
        self, document_id: UUID, dated_only: bool = False
    ) -> list[dict[str, Any]]: ...

    async def get_passage_context(
        self, passage_id: UUID, before: int = 0, after: int = 0
    ) -> dict[str, Any]: ...


@runtime_checkable
class ExtractionClient(Protocol):
    async def extract(
        self, passage_ids: list[UUID], schema: str, options: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def query_records(
        self, record_type: str, filters: dict[str, Any] | None = None, k: int = 100
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class EdgeClient(Protocol):
    async def create(self, edge: dict[str, Any]) -> dict[str, Any]: ...

    async def query(
        self,
        *,
        source_id: UUID | None = None,
        target_id: UUID | None = None,
        relation_type: str | None = None,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class LLMClient(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], model: str | None = None, **opts: Any
    ) -> str: ...

    async def structured(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        model: str | None = None,
        **opts: Any,
    ) -> dict[str, Any]: ...


@runtime_checkable
class HttpClient(Protocol):
    async def get(self, url: str, **kwargs: Any) -> bytes: ...

    async def post(self, url: str, **kwargs: Any) -> bytes: ...


@runtime_checkable
class EntityClient(Protocol):
    async def upsert(self, entity: dict[str, Any]) -> dict[str, Any]: ...

    async def resolve(
        self, name: str, entity_type: str | None = None
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class EventClient(Protocol):
    async def create(self, event: dict[str, Any]) -> Event: ...

    async def query(
        self,
        filters: EventFilter,
        k: int = 1000,
        group_by: str | None = None,
    ) -> tuple[list[Event], list[TimelineBucket]]: ...


@runtime_checkable
class IngestionClient(Protocol):
    async def ingest_paths(
        self, paths: list[Path], hint: str | None = None
    ) -> dict[str, Any]: ...

    async def ingest_document(
        self,
        *,
        title: str,
        document_type: str,
        text: str,
        source: str = "",
        metadata: dict[str, Any] | None = None,
        language: str | None = None,
        sections: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...

    async def ingest_drafts(
        self,
        title: str,
        document_type: str,
        passage_drafts: list[PassageDraft],
        *,
        source: str = "",
        metadata: dict[str, Any] | None = None,
        language: str | None = None,
        full_text: str | None = None,
        node_drafts: list[NodeDraft] | None = None,
    ) -> dict[str, Any]: ...

    async def find_existing(
        self, *, source: str | None = None, source_pattern: str | None = None
    ) -> list[dict[str, Any]]: ...
