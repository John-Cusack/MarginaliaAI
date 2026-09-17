"""Repository port interfaces for all storage access."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID

    from research_engine.domain.citations import (
        BlockCitations,
        CitationItem,
        CitationItemDraft,
        CitationOccurrence,
        OccurrenceDraft,
    )
    from research_engine.domain.claims import (
        Anchor,
        AnchorDraft,
        Claim,
        ClaimAuditReport,
        ClaimDraft,
        ClaimEdge,
        ClaimRelation,
    )
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
        LLMCall,
        LLMCallDraft,
        PluginActivation,
        PluginActivationState,
    )
    from research_engine.domain.spans import SourceSpan
    from research_engine.domain.works import (
        BlockEntityLink,
        BlockEntityLinkDraft,
        BlockLinks,
        BlockSourceLink,
        BlockSourceLinkDraft,
        Edition,
        Waiver,
        WaiverDraft,
        Work,
        WorkBlock,
        WorkBlockDraft,
        WorkDraft,
        WorkRevision,
        WorkRevisionDraft,
    )


class Transaction:
    """Wraps an async database connection within a transaction."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn


@runtime_checkable
class DocumentRepo(Protocol):
    async def insert(self, tx: Transaction, draft: DocumentDraft) -> Document: ...
    async def get(self, doc_id: UUID) -> Document | None: ...
    async def get_many(self, doc_ids: list[UUID]) -> list[Document]: ...
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
    async def parser_versions(self, document_ids: list[UUID]) -> dict[UUID, str]: ...
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
class SourceSpanRepo(Protocol):
    """One row per cited address; every span writer goes through `resolve`."""

    async def resolve(
        self,
        tx: Transaction,
        *,
        document_id: UUID,
        char_start: int,
        char_end: int,
    ) -> SourceSpan: ...
    async def get(self, span_id: UUID) -> SourceSpan | None: ...
    async def for_document(self, document_id: UUID) -> list[SourceSpan]: ...
    async def stale(self, limit: int = 100) -> list[SourceSpan]: ...



@runtime_checkable
class ClaimRepo(Protocol):
    async def upsert_claim(self, tx: Transaction, draft: ClaimDraft) -> Claim: ...
    async def add_edge(
        self,
        tx: Transaction,
        source_id: UUID,
        target_id: UUID,
        relation: ClaimRelation,
        confidence: float | None = None,
        note: str | None = None,
    ) -> ClaimEdge: ...
    async def add_anchor(
        self, tx: Transaction, claim_id: UUID, draft: AnchorDraft
    ) -> Anchor: ...
    async def existing_refs(self, refs: list[str]) -> set[str]: ...
    async def get_by_ref(self, ref: str) -> Claim | None: ...
    async def anchors_for(self, claim_id: UUID) -> list[Anchor]: ...
    async def anchor_by_id(self, anchor_id: UUID) -> Anchor | None: ...
    async def edges_for(self, claim_id: UUID) -> list[ClaimEdge]: ...
    async def audit(self, refs: list[str] | None = None) -> ClaimAuditReport: ...

@runtime_checkable
class EditionRepo(Protocol):
    async def get(self, edition_id: UUID) -> Edition | None: ...
    async def get_by_key(self, edition_key: str) -> Edition | None: ...
    async def upsert_key(
        self, tx: Transaction, edition_key: str, csl: dict | None = None
    ) -> Edition: ...
    async def list_keys(self) -> list[str]: ...


@runtime_checkable
class WorkRepo(Protocol):
    async def insert(self, tx: Transaction, draft: WorkDraft) -> Work: ...
    async def get(self, work_id: UUID) -> Work | None: ...
    async def get_by_slug(self, slug: str) -> Work | None: ...
    async def list(self) -> list[Work]: ...
    async def set_current_revision(
        self, tx: Transaction, work_id: UUID, revision_id: UUID
    ) -> None: ...
    async def update(
        self, tx: Transaction, work_id: UUID, *, expected_updated_at: Any, **fields: Any
    ) -> Work: ...
    async def archive(self, tx: Transaction, work_id: UUID) -> Work: ...


@runtime_checkable
class WorkRevisionRepo(Protocol):
    async def insert(self, tx: Transaction, draft: WorkRevisionDraft) -> WorkRevision: ...
    async def get(self, revision_id: UUID) -> WorkRevision | None: ...
    async def latest(self, work_id: UUID) -> WorkRevision | None: ...
    async def copy_forward(self, tx: Transaction, revision_id: UUID) -> WorkRevision: ...
    async def set_message(
        self, tx: Transaction, revision_id: UUID, message: str
    ) -> WorkRevision: ...
    async def freeze(
        self, tx: Transaction, revision_id: UUID, content_hash: bytes
    ) -> WorkRevision: ...
    async def publish(self, tx: Transaction, revision_id: UUID) -> WorkRevision: ...
    async def supersede(self, tx: Transaction, revision_id: UUID) -> WorkRevision: ...


@runtime_checkable
class WorkBlockRepo(Protocol):
    async def upsert(
        self,
        tx: Transaction,
        revision_id: UUID,
        draft: WorkBlockDraft,
        *,
        expected_updated_at: Any,
    ) -> WorkBlock: ...
    async def tree(self, revision_id: UUID) -> list[WorkBlock]: ...
    async def by_key(self, revision_id: UUID, block_key: UUID) -> WorkBlock | None: ...
    async def delete(self, tx: Transaction, block_id: UUID) -> None: ...


@runtime_checkable
class CitationRepo(Protocol):
    async def insert_occurrence(
        self, tx: Transaction, draft: OccurrenceDraft
    ) -> CitationOccurrence: ...
    async def insert_item(self, tx: Transaction, draft: CitationItemDraft) -> CitationItem: ...
    async def for_block(self, block_id: UUID) -> list[BlockCitations]: ...
    async def for_revision(self, revision_id: UUID) -> list[BlockCitations]: ...
    async def by_key(
        self, revision_id: UUID, citation_key: UUID
    ) -> BlockCitations | None: ...
    async def citing_span(self, span_id: UUID) -> list[BlockCitations]: ...
    async def citing_key(self, edition_key: str) -> list[BlockCitations]: ...


@runtime_checkable
class WorkLinkRepo(Protocol):
    async def add_source_link(
        self, tx: Transaction, draft: BlockSourceLinkDraft
    ) -> BlockSourceLink: ...
    async def add_entity_link(
        self, tx: Transaction, draft: BlockEntityLinkDraft
    ) -> BlockEntityLink: ...
    async def for_block(self, block_id: UUID) -> BlockLinks: ...
    async def for_span(self, span_id: UUID) -> list[BlockSourceLink]: ...
    async def for_entity(
        self, entity_id: UUID, relation: str
    ) -> list[BlockEntityLink]: ...


@runtime_checkable
class WaiverRepo(Protocol):
    async def insert(self, tx: Transaction, draft: WaiverDraft) -> Waiver: ...
    async def for_revision(self, revision_id: UUID) -> list[Waiver]: ...


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
    async def save(self, tx: Transaction, draft: ExtractionSchemaDraft) -> ExtractionSchema: ...
    async def get(self, schema_id: UUID) -> ExtractionSchema | None: ...
    async def get_by_name_version(
        self, name: str, version: int
    ) -> ExtractionSchema | None: ...
    async def list_all(self) -> list[ExtractionSchema]: ...


@runtime_checkable
class ExtractionRepo(Protocol):
    async def save(self, tx: Transaction, extraction: Extraction) -> Extraction: ...
    async def get_by_key(
        self, passage_id: UUID, schema_id: UUID, extractor_version: str
    ) -> Extraction | None: ...
    async def replace_records(
        self, tx: Transaction, extraction_id: UUID, records: list[ExtractionRecord]
    ) -> None: ...
    async def get_record(self, record_id: UUID) -> ExtractionRecord | None: ...
    async def query_records(
        self,
        record_type: str,
        data_filter: dict[str, Any] | None = None,
        passage_ids: list[UUID] | None = None,
        k: int = 100,
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
class PluginActivationRepo(Protocol):
    async def save(self, activation: PluginActivation) -> None: ...
    async def get(self, plugin_id: str) -> PluginActivation | None: ...
    async def list_enabled(self) -> list[PluginActivation]: ...
    async def list_all(self) -> list[PluginActivation]: ...
    async def update_state(
        self,
        plugin_id: str,
        state: PluginActivationState,
        *,
        enabled: bool | None = None,
        last_error: str | None = None,
        last_seen_at: Any | None = None,
    ) -> None: ...
    async def record_migration(
        self,
        plugin_id: str,
        *,
        revision: int,
        status: str,
        state: PluginActivationState,
        last_error: str | None = None,
    ) -> None: ...
    async def delete(self, plugin_id: str) -> None: ...
