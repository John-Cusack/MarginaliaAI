"""Permission-scoped service protocols injected into plugin handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import date, datetime
    from pathlib import Path
    from uuid import UUID

    from research_engine_sdk.types import (
        Event,
        EventFilter,
        MarkDraft,
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
        parser: str = "plugin_direct",
        parser_version: str = "1.0",
        mark_drafts: list[MarkDraft] | None = None,
    ) -> dict[str, Any]: ...

    async def find_existing(
        self, *, source: str | None = None, source_pattern: str | None = None
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class BibliographyClient(Protocol):
    """Where each edition came from, on what terms, and whether it all arrived.

    Granted with the ``ingest`` permission. Every value — an acquisition
    method, a licence — is the plugin's own vocabulary; core stores the shape
    and enforces only the lifecycle (`planned`, `acquired`, `loaded`,
    `blocked`, `failed`) and that a blocked source says why.
    """

    async def register_source(
        self,
        edition_key: str,
        *,
        csl: dict[str, Any] | None = None,
        work_key: str | None = None,
        collection: str | None = None,
        status: str | None = None,
        method: str | None = None,
        url: str | None = None,
        license: str | None = None,  # noqa: A002 - the field's name
        license_notes: str | None = None,
        retrieved_at: datetime | str | None = None,
        blocked_reason: str | None = None,
        suggested_acquisition: str | None = None,
        manifest_ref: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def mark_blocked(
        self,
        edition_key: str,
        *,
        reason: str,
        suggested_acquisition: str,
        **declared: Any,
    ) -> dict[str, Any]: ...

    async def set_status(
        self,
        edition_key: str,
        status: str,
        *,
        retrieved_at: datetime | str | None = None,
    ) -> dict[str, Any]: ...

    async def attach_artifact(
        self,
        edition_key: str,
        path: str | Path,
        *,
        sha256: str | None = None,
        url: str | None = None,
        media_type: str | None = None,
        retrieved_at: datetime | str | None = None,
    ) -> dict[str, Any]: ...

    async def report_coverage(
        self,
        edition_key: str,
        *,
        unit: str,
        loaded_units: int,
        checker: str,
        checker_version: str,
        expected_units: int | None = None,
        anomalies: list[Any] | None = None,
    ) -> dict[str, Any]: ...

    async def get_source(self, edition_key: str) -> dict[str, Any] | None: ...

    async def list_sources(
        self, *, status: str | None = None, collection: str | None = None
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class ProfileClient(Protocol):
    """A reader's view of the library, and the runs made under it.

    Granted with the ``ingest`` permission. A profile gives each edition a
    role — ``binding``, ``external``, ``reference``, ``defense``,
    ``critical`` or ``fact`` — optionally with a tier (1–9), a date window
    (``valid_from`` inclusive, ``valid_to`` exclusive) and a ``scope`` that
    narrows it to part of the edition. Each role is a dict with those keys
    plus ``edition_key``. Loading replaces the profile's roles whole.
    """

    async def load_profile(
        self,
        key: str,
        *,
        version: int,
        profile_hash: str,
        roles: list[dict[str, Any]],
        title: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def get_profile(
        self, key: str, *, on: date | str | None = None
    ) -> dict[str, Any] | None: ...

    async def start_run(
        self,
        profile_key: str,
        *,
        stage: str,
        config: dict[str, Any] | None = None,
        code_version: str | None = None,
        model_versions: dict[str, Any] | None = None,
        items_in: int | None = None,
        cost_estimate: float | None = None,
    ) -> dict[str, Any]: ...

    async def finish_run(
        self,
        run_id: str | UUID,
        *,
        status: str = "finished",
        items_out: int | None = None,
        stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
