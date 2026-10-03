"""Permission-scoped service protocols injected into plugin handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime
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

    async def get_document(self, document_id: UUID) -> dict[str, Any] | None:
        """The document's fields and its passages, each with ``char_start`` /
        ``char_end`` into the canonical text."""
        ...

    async def get_document_text(self, document_id: UUID) -> str | None:
        """The canonical text passage offsets index into."""
        ...

    async def find_documents(
        self,
        *,
        document_types: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        source_pattern: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Documents by type, metadata containment and source substring; no
        passages. At least one criterion is required."""
        ...

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
        self,
        record_type: str,
        filters: dict[str, Any] | None = None,
        k: int = 100,
        *,
        passage_ids: list[UUID | str] | None = None,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
        """Stored records of *record_type*.

        *filters* match the record data by containment; *passage_ids* narrow to
        those passages. *schema* (``"name:version"``) scopes the read to that
        schema version and, per passage, to its most recent successful
        extraction — so a re-extraction replaces what is read instead of adding
        to it.
        """
        ...


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
    """Events and their actors.

    Writes (``create``, ``upsert``, ``delete``) need the ``write`` permission;
    reads do not. Direction lives in each actor's ``role`` — ``sender`` and
    ``recipient`` for a letter — so read it from the actors, never from a copy
    in the payload.
    """

    async def create(self, event: dict[str, Any]) -> Event: ...

    async def upsert(self, event: dict[str, Any]) -> Event:
        """Create or replace the one event of ``event_type`` derived from
        ``source_passage_id`` (required). ``actors``, when given, replace the
        stored ones."""
        ...

    async def delete(self, event_id: UUID | str) -> bool: ...

    async def query(
        self,
        filters: EventFilter,
        k: int = 1000,
        group_by: str | None = None,
    ) -> tuple[list[Event], list[TimelineBucket]]: ...

    async def get_actors(self, event_id: UUID | str) -> list[dict[str, Any]]: ...

    async def get_actors_many(
        self, event_ids: list[UUID | str]
    ) -> dict[str, list[dict[str, Any]]]:
        """``{event_id: [{"entity_id", "role"}, ...]}`` in one round trip."""
        ...


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
        created_date_start: datetime | str | None = None,
        created_date_end: datetime | str | None = None,
        created_precision: str | None = None,
        edition_id: UUID | str | None = None,
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

    async def update_document(
        self,
        document_id: UUID | str,
        *,
        title: str | None = None,
        document_type: str | None = None,
        created_date_start: datetime | str | None = None,
        created_date_end: datetime | str | None = None,
        created_precision: str | None = None,
        clear_created_date: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Change title, type, date or metadata (merged); never content."""
        ...

    async def delete_document(self, document_id: UUID | str) -> bool:
        """Delete a document the pack derived and is replacing."""
        ...
