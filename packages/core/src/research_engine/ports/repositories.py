"""Repository port interfaces for all storage access."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID

    from research_engine.domain.documents import (
        Document,
        DocumentDraft,
        DocumentFilter,
        DocumentText,
    )
    from research_engine.domain.edges import Edge, EdgeDraft
    from research_engine.domain.entities import (
        Entity,
        EntityAlias,
        EntityCandidate,
        EntityDraft,
        Mention,
        MentionDraft,
    )
    from research_engine.domain.events import Event, EventActor, EventDraft, EventFilter
    from research_engine.domain.extractions import (
        Extraction,
        ExtractionRecord,
        ExtractionSchema,
        ExtractionSchemaDraft,
    )
    from research_engine.domain.filter_extension import FilterExtension
    from research_engine.domain.passages import Passage, PassageDraft
    from research_engine.domain.provenance import (
        IngestionItem,
        IngestionRun,
        InstalledPlugin,
        LLMCall,
        LLMCallDraft,
    )


class Transaction:
    """Wraps an async database connection within a transaction."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn


@runtime_checkable
class DocumentRepo(Protocol):
    async def insert(self, tx: Transaction, draft: DocumentDraft) -> Document: ...
    async def get(self, doc_id: UUID) -> Document | None: ...
    async def find_by_hash(self, content_hash: bytes, source: str) -> Document | None: ...
    async def update_metadata(self, doc_id: UUID, patch: dict[str, Any]) -> Document: ...
    async def iter_by_filter(self, filter: DocumentFilter) -> AsyncIterator[Document]: ...
    async def count(self, filter: DocumentFilter | None = None) -> int: ...
    async def delete(self, doc_id: UUID) -> None: ...


@runtime_checkable
class DocumentTextRepo(Protocol):
    """Canonical document text — the substrate passage offsets address."""

    async def put(
        self,
        tx: Transaction,
        document_id: UUID,
        text: str,
        parser: str,
        parser_version: str,
    ) -> None: ...
    async def get(self, document_id: UUID) -> DocumentText | None: ...
    async def get_text(self, document_id: UUID) -> str | None: ...
    async def missing_document_ids(self, limit: int | None = None) -> list[UUID]: ...
    async def count(self) -> int: ...


@runtime_checkable
class PassageRepo(Protocol):
    async def insert_many(
        self, tx: Transaction, document_id: UUID, drafts: list[PassageDraft]
    ) -> list[Passage]: ...
    async def get(self, passage_id: UUID) -> Passage | None: ...
    async def get_by_document(self, document_id: UUID) -> list[Passage]: ...
    async def get_context(
        self, passage_id: UUID, before: int = 0, after: int = 0
    ) -> tuple[list[Passage], Passage, list[Passage]]: ...
    async def vector_search(
        self,
        query_embedding: list[float],
        model: str,
        model_version: str,
        candidate_ids: list[UUID] | None,
        k: int,
    ) -> list[tuple[UUID, float]]: ...
    async def keyword_search(
        self,
        query: str,
        lang: str | None,
        candidate_ids: list[UUID] | None,
        k: int,
    ) -> list[tuple[UUID, float]]: ...
    async def store_embeddings(
        self,
        tx: Transaction,
        passage_ids: list[UUID],
        embeddings: list[list[float]],
        model: str,
        model_version: str,
        dim: int,
    ) -> None: ...
    async def index_fts(
        self, tx: Transaction, passage_ids: list[UUID], texts: list[str], lang: str
    ) -> None: ...
    async def get_embedding(
        self, passage_id: UUID, model: str, model_version: str
    ) -> list[float] | None: ...
    async def filter_candidate_ids(
        self, filters: dict[str, Any],
        filter_extensions: dict[str, FilterExtension] | None = None,
    ) -> list[UUID]: ...
    async def count(self) -> int: ...


@runtime_checkable
class EntityRepo(Protocol):
    async def insert(self, tx: Transaction, draft: EntityDraft) -> Entity: ...
    async def get(self, entity_id: UUID) -> Entity | None: ...
    async def update(self, entity_id: UUID, patch: dict[str, Any]) -> Entity: ...
    async def search_by_name(
        self, query: str, entity_type: str | None, k: int
    ) -> list[EntityCandidate]: ...
    async def get_aliases(self, entity_id: UUID) -> list[EntityAlias]: ...
    async def add_alias(self, tx: Transaction, alias: EntityAlias) -> None: ...
    async def list_by_type(self, entity_type: str, limit: int = 100) -> list[Entity]: ...
    async def count(self) -> int: ...


@runtime_checkable
class MentionRepo(Protocol):
    async def insert(self, tx: Transaction, draft: MentionDraft) -> Mention: ...
    async def insert_many(self, tx: Transaction, drafts: list[MentionDraft]) -> list[Mention]: ...
    async def get_by_passage(self, passage_id: UUID) -> list[Mention]: ...
    async def get_by_entity(
        self, entity_id: UUID, filters: dict[str, Any] | None, k: int
    ) -> list[Mention]: ...


@runtime_checkable
class EventRepo(Protocol):
    async def insert(self, tx: Transaction, draft: EventDraft) -> Event: ...
    async def get(self, event_id: UUID) -> Event | None: ...
    async def query(self, filter: EventFilter, k: int) -> list[Event]: ...
    async def get_actors(self, event_id: UUID) -> list[EventActor]: ...
    async def add_actor(self, tx: Transaction, actor: EventActor) -> None: ...
    async def count(self, filter: EventFilter | None = None) -> int: ...


@runtime_checkable
class EdgeRepo(Protocol):
    async def insert(self, tx: Transaction, draft: EdgeDraft) -> Edge: ...
    async def get(self, edge_id: UUID) -> Edge | None: ...
    async def query_by_source(
        self, source_kind: str, source_id: UUID, relation_type: str | None
    ) -> list[Edge]: ...
    async def query_by_target(
        self, target_kind: str, target_id: UUID, relation_type: str | None
    ) -> list[Edge]: ...


@runtime_checkable
class ExtractionSchemaRepo(Protocol):
    async def insert(self, tx: Transaction, draft: ExtractionSchemaDraft) -> ExtractionSchema: ...
    async def get(self, schema_id: UUID) -> ExtractionSchema | None: ...
    async def get_by_name_version(
        self, name: str, version: int
    ) -> ExtractionSchema | None: ...
    async def list_all(self) -> list[ExtractionSchema]: ...


@runtime_checkable
class ExtractionRepo(Protocol):
    async def insert(self, tx: Transaction, extraction: Extraction) -> Extraction: ...
    async def get_by_key(
        self, passage_id: UUID, schema_id: UUID, extractor_version: str
    ) -> Extraction | None: ...
    async def insert_records(
        self, tx: Transaction, records: list[ExtractionRecord]
    ) -> None: ...
    async def query_records(
        self, record_type: str, filters: dict[str, Any] | None, k: int
    ) -> list[ExtractionRecord]: ...


@runtime_checkable
class LLMCallLogRepo(Protocol):
    async def insert(self, draft: LLMCallDraft) -> LLMCall: ...
    async def get(self, call_id: UUID) -> LLMCall | None: ...
    async def recent(self, limit: int = 100) -> list[LLMCall]: ...


@runtime_checkable
class IngestionRunRepo(Protocol):
    async def start_run(self, source_spec: dict[str, Any]) -> IngestionRun: ...
    async def complete_run(
        self, run_id: UUID, status: str, stats: dict[str, Any]
    ) -> IngestionRun: ...
    async def add_item(
        self, run_id: UUID, source_ref: str, status: str, **kwargs: Any
    ) -> IngestionItem: ...
    async def update_item(self, item_id: UUID, **kwargs: Any) -> IngestionItem: ...


@runtime_checkable
class InstalledPluginRepo(Protocol):
    async def insert(self, plugin: InstalledPlugin) -> None: ...
    async def get(self, plugin_id: str) -> InstalledPlugin | None: ...
    async def list_enabled(self) -> list[InstalledPlugin]: ...
    async def list_all(self) -> list[InstalledPlugin]: ...
    async def set_enabled(self, plugin_id: str, enabled: bool) -> None: ...
    async def delete(self, plugin_id: str) -> None: ...
