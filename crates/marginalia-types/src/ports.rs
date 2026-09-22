//! Port traits mirroring `ports/repositories.py` (storage) and
//! `ports/embedding.py`, `ports/llm.py`, `ports/reranker.py`, `ports/http.py`,
//! `ports/clock.py` (services), plus the `FilterExtension` protocol from
//! `domain/filter_extension.py`.
//!
//! Mapping notes:
//! - Python's `Transaction` (a wrapper around one async connection) becomes a
//!   per-trait associated `Tx` type. Adapters pick the concrete handle in
//!   Phase 5; test fakes use an in-memory stand-in. Method shapes are
//!   otherwise 1:1 with the Protocols.
//! - `DocumentRepo.iter_by_filter` is an async generator in Python; here it
//!   collects into a `Vec`. Phase 5 may reintroduce streaming if profiling
//!   needs it.
//! - `**kwargs: Any` catch-alls (`WorkRepo.update`, `IngestionRunRepo.*_item`)
//!   become explicit `Map<String, Value>` update maps.
//! - `FilterExtension.build_clause` returns SQL in Python; the clause type is
//!   associated so Phase 5 can bind it to its query builder.

use std::collections::HashMap;

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::citations::{
    BlockCitations, CitationItem, CitationItemDraft, CitationOccurrence, OccurrenceDraft,
};
use crate::claims::{
    Anchor, AnchorDraft, Claim, ClaimAuditReport, ClaimDraft, ClaimEdge, ClaimRelation,
};
use crate::documents::{Document, DocumentDraft, DocumentFilter, DocumentText};
use crate::edges::{Edge, EdgeDraft};
use crate::entities::{Entity, EntityAlias, EntityCandidate, EntityDraft, Mention, MentionDraft};
use crate::errors::{Error, Result};
use crate::events::{Event, EventActor, EventDraft, EventFilter};
use crate::extractions::{Extraction, ExtractionRecord, ExtractionSchema, ExtractionSchemaDraft};
use crate::passages::{Passage, PassageDraft};
use crate::provenance::{
    IngestionItem, IngestionRun, LlmCall, LlmCallDraft, PluginActivation, PluginActivationState,
};
use crate::spans::SourceSpan;
use crate::works::{
    BlockEntityLink, BlockEntityLinkDraft, BlockLinks, BlockSourceLink, BlockSourceLinkDraft,
    Edition, Waiver, WaiverDraft, Work, WorkBlock, WorkBlockDraft, WorkDraft, WorkRevision,
    WorkRevisionDraft,
};

// --- Filter extensions ---

/// A pluggable search filter narrowing passage candidates.
///
/// Plugins register implementations; core composes them into the
/// filter-pushdown stage of hybrid search. Must use bind parameters, never
/// string interpolation.
pub trait FilterExtension: Send + Sync {
    /// The clause type — bound to the query builder in Phase 5.
    type Clause: Send;

    /// Unique ID, e.g. `scripture_ref_range` or `event_date_range`.
    fn filter_id(&self) -> &str;

    /// JSON Schema for the filter value the LLM provides.
    fn input_schema(&self) -> Map<String, Value>;

    /// Human-readable description so the LLM knows when to use this filter.
    fn description(&self) -> &str;

    /// A SELECT yielding matching passage-id rows for `value`.
    fn build_clause(&self, value: &Value) -> Result<Self::Clause>;
}

// --- Storage repositories ---

pub trait DocumentRepo: Send + Sync {
    type Tx: Send;
    async fn insert(&self, tx: &mut Self::Tx, draft: DocumentDraft) -> Result<Document>;
    async fn get(&self, doc_id: Uuid) -> Result<Option<Document>>;
    async fn get_many(&self, doc_ids: &[Uuid]) -> Result<Vec<Document>>;
    async fn find_by_hash(&self, content_hash: &[u8], source: &str) -> Result<Option<Document>>;
    /// An already-ingested artifact while its edition row is locked: runs
    /// inside the caller's transaction, never the pool.
    async fn find_by_edition_id(
        &self,
        tx: &mut Self::Tx,
        edition_id: Uuid,
    ) -> Result<Option<Document>>;
    async fn update_metadata(&self, doc_id: Uuid, patch: Map<String, Value>) -> Result<Document>;
    async fn iter_by_filter(&self, filter: &DocumentFilter) -> Result<Vec<Document>>;
    async fn count(&self, filter: Option<&DocumentFilter>) -> Result<i64>;
    async fn delete(&self, doc_id: Uuid) -> Result<()>;
}

/// Canonical document text — the substrate passage offsets address.
pub trait DocumentTextRepo: Send + Sync {
    type Tx: Send;
    async fn put(
        &self,
        tx: &mut Self::Tx,
        document_id: Uuid,
        text: &str,
        parser: &str,
        parser_version: &str,
    ) -> Result<()>;
    async fn get(&self, document_id: Uuid) -> Result<Option<DocumentText>>;
    async fn get_text(&self, document_id: Uuid) -> Result<Option<String>>;
    async fn parser_versions(&self, document_ids: &[Uuid]) -> Result<HashMap<Uuid, String>>;
    /// `(raw_length, normalized_length)`, or `None` with no canonical text.
    /// `WorkVerifier` reads only the `None`-ness; the lengths feed the
    /// offset-mapping window upstream.
    async fn lengths(&self, document_id: Uuid) -> Result<Option<(i64, i64)>>;
    async fn missing_document_ids(&self, limit: Option<i64>) -> Result<Vec<Uuid>>;
}

pub trait PassageRepo: Send + Sync {
    type Tx: Send;
    async fn insert_many(
        &self,
        tx: &mut Self::Tx,
        document_id: Uuid,
        drafts: Vec<PassageDraft>,
    ) -> Result<Vec<Passage>>;
    async fn get(&self, passage_id: Uuid) -> Result<Option<Passage>>;
    async fn get_by_document(&self, document_id: Uuid) -> Result<Vec<Passage>>;
    async fn get_context(
        &self,
        passage_id: Uuid,
        before: i64,
        after: i64,
    ) -> Result<(Vec<Passage>, Passage, Vec<Passage>)>;
    async fn vector_search(
        &self,
        query_embedding: &[f64],
        model: &str,
        model_version: &str,
        candidate_ids: Option<&[Uuid]>,
        k: i64,
    ) -> Result<Vec<(Uuid, f64)>>;
    async fn keyword_search(
        &self,
        query: &str,
        lang: Option<&str>,
        candidate_ids: Option<&[Uuid]>,
        k: i64,
    ) -> Result<Vec<(Uuid, f64)>>;
    async fn store_embeddings(
        &self,
        tx: &mut Self::Tx,
        passage_ids: &[Uuid],
        embeddings: &[Vec<f64>],
        model: &str,
        model_version: &str,
        dim: i64,
    ) -> Result<()>;
    async fn index_fts(
        &self,
        tx: &mut Self::Tx,
        passage_ids: &[Uuid],
        texts: &[String],
        lang: &str,
    ) -> Result<()>;
    async fn get_embedding(
        &self,
        passage_id: Uuid,
        model: &str,
        model_version: &str,
    ) -> Result<Option<Vec<f64>>>;
    async fn filter_candidate_ids<F: FilterExtension>(
        &self,
        filters: &Map<String, Value>,
        filter_extensions: Option<&HashMap<String, F>>,
    ) -> Result<Vec<Uuid>>;
    /// Passages overlapping a span; the narrowed/region rules compare exact
    /// bounds against these candidates.
    async fn covering_span(
        &self,
        document_id: Uuid,
        char_start: i64,
        char_end: i64,
    ) -> Result<Vec<Passage>>;
    async fn count(&self) -> Result<i64>;
}

/// One row per cited address; every span writer goes through `resolve`.
pub trait SourceSpanRepo: Send + Sync {
    type Tx: Send;
    async fn resolve(
        &self,
        tx: &mut Self::Tx,
        document_id: Uuid,
        char_start: i64,
        char_end: i64,
    ) -> Result<SourceSpan>;
    async fn get(&self, span_id: Uuid) -> Result<Option<SourceSpan>>;
    async fn for_document(&self, document_id: Uuid) -> Result<Vec<SourceSpan>>;
    async fn stale(&self, limit: i64) -> Result<Vec<SourceSpan>>;
}

pub trait ClaimRepo: Send + Sync {
    type Tx: Send;
    async fn upsert_claim(&self, tx: &mut Self::Tx, draft: ClaimDraft) -> Result<Claim>;
    async fn add_edge(
        &self,
        tx: &mut Self::Tx,
        source_id: Uuid,
        target_id: Uuid,
        relation: ClaimRelation,
        confidence: Option<f64>,
        note: Option<&str>,
    ) -> Result<ClaimEdge>;
    async fn add_anchor(
        &self,
        tx: &mut Self::Tx,
        claim_id: Uuid,
        draft: AnchorDraft,
    ) -> Result<Anchor>;
    async fn existing_refs(&self, refs: &[String]) -> Result<std::collections::HashSet<String>>;
    async fn get_by_ref(&self, ref_: &str) -> Result<Option<Claim>>;
    async fn anchors_for(&self, claim_id: Uuid) -> Result<Vec<Anchor>>;
    async fn anchor_by_id(&self, anchor_id: Uuid) -> Result<Option<Anchor>>;
    async fn edges_for(&self, claim_id: Uuid) -> Result<Vec<ClaimEdge>>;
    async fn audit(&self, refs: Option<&[String]>) -> Result<ClaimAuditReport>;
}

pub trait EditionRepo: Send + Sync {
    type Tx: Send;
    async fn get(&self, edition_id: Uuid) -> Result<Option<Edition>>;
    async fn get_by_key(&self, edition_key: &str) -> Result<Option<Edition>>;
    async fn upsert_key(
        &self,
        tx: &mut Self::Tx,
        edition_key: &str,
        csl: Option<Map<String, Value>>,
        lock: bool,
    ) -> Result<Edition>;
    async fn list_keys(&self) -> Result<Vec<String>>;
}

pub trait WorkRepo: Send + Sync {
    type Tx: Send;
    async fn insert(&self, tx: &mut Self::Tx, draft: WorkDraft) -> Result<Work>;
    async fn get(&self, work_id: Uuid) -> Result<Option<Work>>;
    async fn get_by_slug(&self, slug: &str) -> Result<Option<Work>>;
    async fn list(&self) -> Result<Vec<Work>>;
    async fn set_current_revision(
        &self,
        tx: &mut Self::Tx,
        work_id: Uuid,
        revision_id: Uuid,
    ) -> Result<()>;
    async fn update(
        &self,
        tx: &mut Self::Tx,
        work_id: Uuid,
        expected_updated_at: DateTime<Utc>,
        fields: Map<String, Value>,
    ) -> Result<Work>;
    async fn archive(&self, tx: &mut Self::Tx, work_id: Uuid) -> Result<Work>;
}

pub trait WorkRevisionRepo: Send + Sync {
    type Tx: Send;
    async fn insert(&self, tx: &mut Self::Tx, draft: WorkRevisionDraft) -> Result<WorkRevision>;
    async fn get(&self, revision_id: Uuid) -> Result<Option<WorkRevision>>;
    async fn latest(&self, work_id: Uuid) -> Result<Option<WorkRevision>>;
    async fn copy_forward(&self, tx: &mut Self::Tx, revision_id: Uuid) -> Result<WorkRevision>;
    async fn set_message(
        &self,
        tx: &mut Self::Tx,
        revision_id: Uuid,
        message: &str,
    ) -> Result<WorkRevision>;
    async fn freeze(
        &self,
        tx: &mut Self::Tx,
        revision_id: Uuid,
        content_hash: &[u8],
    ) -> Result<WorkRevision>;
    async fn publish(&self, tx: &mut Self::Tx, revision_id: Uuid) -> Result<WorkRevision>;
    async fn supersede(&self, tx: &mut Self::Tx, revision_id: Uuid) -> Result<WorkRevision>;
}

pub trait WorkBlockRepo: Send + Sync {
    type Tx: Send;
    /// `expected_updated_at` is `None` for inserts (no veteran row to check).
    async fn upsert(
        &self,
        tx: &mut Self::Tx,
        revision_id: Uuid,
        draft: WorkBlockDraft,
        expected_updated_at: Option<DateTime<Utc>>,
    ) -> Result<WorkBlock>;
    async fn tree(&self, revision_id: Uuid) -> Result<Vec<WorkBlock>>;
    async fn get(&self, block_id: Uuid) -> Result<Option<WorkBlock>>;
    async fn by_key(&self, revision_id: Uuid, block_key: Uuid) -> Result<Option<WorkBlock>>;
    /// Same-row read inside an open write transaction (draft import).
    async fn by_key_in_tx(
        &self,
        tx: &mut Self::Tx,
        revision_id: Uuid,
        block_key: Uuid,
    ) -> Result<Option<WorkBlock>>;
    async fn delete(&self, tx: &mut Self::Tx, block_id: Uuid) -> Result<()>;
}

pub trait CitationRepo: Send + Sync {
    type Tx: Send;
    async fn insert_occurrence(
        &self,
        tx: &mut Self::Tx,
        draft: OccurrenceDraft,
    ) -> Result<CitationOccurrence>;
    async fn insert_item(
        &self,
        tx: &mut Self::Tx,
        draft: CitationItemDraft,
    ) -> Result<CitationItem>;
    async fn for_block(&self, block_id: Uuid) -> Result<Vec<BlockCitations>>;
    async fn for_revision(&self, revision_id: Uuid) -> Result<Vec<BlockCitations>>;
    async fn by_key(&self, revision_id: Uuid, citation_key: Uuid)
        -> Result<Option<BlockCitations>>;
    async fn citing_span(&self, span_id: Uuid) -> Result<Vec<BlockCitations>>;
    async fn citing_key(&self, edition_key: &str) -> Result<Vec<BlockCitations>>;
}

pub trait WorkLinkRepo: Send + Sync {
    type Tx: Send;
    async fn add_source_link(
        &self,
        tx: &mut Self::Tx,
        draft: BlockSourceLinkDraft,
    ) -> Result<BlockSourceLink>;
    async fn add_entity_link(
        &self,
        tx: &mut Self::Tx,
        draft: BlockEntityLinkDraft,
    ) -> Result<BlockEntityLink>;
    async fn for_block(&self, block_id: Uuid) -> Result<BlockLinks>;
    async fn for_span(&self, span_id: Uuid) -> Result<Vec<BlockSourceLink>>;
    async fn for_entity(&self, entity_id: Uuid, relation: &str) -> Result<Vec<BlockEntityLink>>;
}

pub trait WaiverRepo: Send + Sync {
    type Tx: Send;
    async fn insert(&self, tx: &mut Self::Tx, draft: WaiverDraft) -> Result<Waiver>;
    async fn for_revision(&self, revision_id: Uuid) -> Result<Vec<Waiver>>;
}

pub trait EntityRepo: Send + Sync {
    type Tx: Send;
    async fn insert(&self, tx: &mut Self::Tx, draft: EntityDraft) -> Result<Entity>;
    async fn get(&self, entity_id: Uuid) -> Result<Option<Entity>>;
    async fn update(&self, entity_id: Uuid, patch: Map<String, Value>) -> Result<Entity>;
    async fn search_by_name(
        &self,
        query: &str,
        entity_type: Option<&str>,
        k: i64,
    ) -> Result<Vec<EntityCandidate>>;
    async fn get_aliases(&self, entity_id: Uuid) -> Result<Vec<EntityAlias>>;
    async fn add_alias(&self, tx: &mut Self::Tx, alias: EntityAlias) -> Result<()>;
    async fn list_by_type(&self, entity_type: &str, limit: i64) -> Result<Vec<Entity>>;
    async fn count(&self) -> Result<i64>;
}

pub trait MentionRepo: Send + Sync {
    type Tx: Send;
    async fn insert(&self, tx: &mut Self::Tx, draft: MentionDraft) -> Result<Mention>;
    async fn insert_many(
        &self,
        tx: &mut Self::Tx,
        drafts: Vec<MentionDraft>,
    ) -> Result<Vec<Mention>>;
    async fn get_by_passage(&self, passage_id: Uuid) -> Result<Vec<Mention>>;
    async fn get_by_entity(
        &self,
        entity_id: Uuid,
        filters: Option<&Map<String, Value>>,
        k: i64,
    ) -> Result<Vec<Mention>>;
}

pub trait EventRepo: Send + Sync {
    type Tx: Send;
    async fn insert(&self, tx: &mut Self::Tx, draft: EventDraft) -> Result<Event>;
    async fn get(&self, event_id: Uuid) -> Result<Option<Event>>;
    async fn query(&self, filter: &EventFilter, k: i64) -> Result<Vec<Event>>;
    async fn get_actors(&self, event_id: Uuid) -> Result<Vec<EventActor>>;
    async fn add_actor(&self, tx: &mut Self::Tx, actor: EventActor) -> Result<()>;
    async fn count(&self, filter: Option<&EventFilter>) -> Result<i64>;
}

pub trait EdgeRepo: Send + Sync {
    type Tx: Send;
    async fn insert(&self, tx: &mut Self::Tx, draft: EdgeDraft) -> Result<Edge>;
    async fn get(&self, edge_id: Uuid) -> Result<Option<Edge>>;
    async fn query_by_source(
        &self,
        source_kind: &str,
        source_id: Uuid,
        relation_type: Option<&str>,
    ) -> Result<Vec<Edge>>;
    async fn query_by_target(
        &self,
        target_kind: &str,
        target_id: Uuid,
        relation_type: Option<&str>,
    ) -> Result<Vec<Edge>>;
}

pub trait ExtractionSchemaRepo: Send + Sync {
    type Tx: Send;
    async fn save(
        &self,
        tx: &mut Self::Tx,
        draft: ExtractionSchemaDraft,
    ) -> Result<ExtractionSchema>;
    async fn get(&self, schema_id: Uuid) -> Result<Option<ExtractionSchema>>;
    async fn get_by_name_version(
        &self,
        name: &str,
        version: i64,
    ) -> Result<Option<ExtractionSchema>>;
    async fn list_all(&self) -> Result<Vec<ExtractionSchema>>;
}

pub trait ExtractionRepo: Send + Sync {
    type Tx: Send;
    async fn save(&self, tx: &mut Self::Tx, extraction: Extraction) -> Result<Extraction>;
    async fn get_by_key(
        &self,
        passage_id: Uuid,
        schema_id: Uuid,
        extractor_version: &str,
    ) -> Result<Option<Extraction>>;
    async fn replace_records(
        &self,
        tx: &mut Self::Tx,
        extraction_id: Uuid,
        records: Vec<ExtractionRecord>,
    ) -> Result<()>;
    async fn get_record(&self, record_id: Uuid) -> Result<Option<ExtractionRecord>>;
    async fn query_records(
        &self,
        record_type: &str,
        data_filter: Option<&Map<String, Value>>,
        passage_ids: Option<&[Uuid]>,
        k: i64,
    ) -> Result<Vec<ExtractionRecord>>;
}

pub trait LlmCallLogRepo: Send + Sync {
    async fn insert(&self, draft: LlmCallDraft) -> Result<LlmCall>;
    async fn get(&self, call_id: Uuid) -> Result<Option<LlmCall>>;
    async fn recent(&self, limit: i64) -> Result<Vec<LlmCall>>;
}

pub trait IngestionRunRepo: Send + Sync {
    async fn start_run(&self, source_spec: Map<String, Value>) -> Result<IngestionRun>;
    async fn complete_run(
        &self,
        run_id: Uuid,
        status: &str,
        stats: Map<String, Value>,
    ) -> Result<IngestionRun>;
    async fn add_item(
        &self,
        run_id: Uuid,
        source_ref: &str,
        status: &str,
        extra: Map<String, Value>,
    ) -> Result<IngestionItem>;
    async fn update_item(
        &self,
        item_id: Uuid,
        updates: Map<String, Value>,
    ) -> Result<IngestionItem>;
}

pub trait PluginActivationRepo: Send + Sync {
    async fn save(&self, activation: PluginActivation) -> Result<()>;
    async fn get(&self, plugin_id: &str) -> Result<Option<PluginActivation>>;
    async fn list_enabled(&self) -> Result<Vec<PluginActivation>>;
    async fn list_all(&self) -> Result<Vec<PluginActivation>>;
    async fn update_state(
        &self,
        plugin_id: &str,
        state: PluginActivationState,
        enabled: Option<bool>,
        last_error: Option<&str>,
        last_seen_at: Option<DateTime<Utc>>,
    ) -> Result<()>;
    async fn record_migration(
        &self,
        plugin_id: &str,
        revision: i64,
        status: &str,
        state: PluginActivationState,
        last_error: Option<&str>,
    ) -> Result<()>;
    async fn delete(&self, plugin_id: &str) -> Result<()>;
}

// --- Service ports ---

pub trait EmbeddingPort: Send + Sync {
    fn model_name(&self) -> &str;
    fn model_version(&self) -> &str;
    fn dim(&self) -> i64;
    /// Embed a single text.
    async fn embed(&self, text: &str) -> Result<Vec<f64>, Error>;
    /// Embed a batch of texts.
    async fn embed_batch(&self, texts: &[String]) -> Result<Vec<Vec<f64>>, Error>;
}

pub trait RerankerPort: Send + Sync {
    /// Rerank passages by relevance to query. Returns `(passage_id, score)`.
    async fn rerank(
        &self,
        query: &str,
        passage_ids: &[Uuid],
        texts: &[String],
        k: i64,
    ) -> Result<Vec<(Uuid, f64)>, Error>;
}

/// A chat message: role/content string pairs.
pub type ChatMessage = HashMap<String, String>;

pub trait LlmPort: Send + Sync {
    /// Complete a prompt. Returns `(response_text, llm_call_id)`.
    async fn complete(
        &self,
        messages: &[ChatMessage],
        model: Option<&str>,
        caller: &str,
        purpose: &str,
    ) -> Result<(String, Uuid), Error>;
    /// Complete with structured output. Returns `(parsed_json, llm_call_id)`.
    async fn structured(
        &self,
        messages: &[ChatMessage],
        schema: &Map<String, Value>,
        model: Option<&str>,
        caller: &str,
        purpose: &str,
    ) -> Result<(Map<String, Value>, Uuid), Error>;
}

pub trait HttpPort: Send + Sync {
    /// Fetch bytes from a URL.
    async fn get(&self, url: &str, options: Map<String, Value>) -> Result<Vec<u8>, Error>;
    /// POST to a URL.
    async fn post(
        &self,
        url: &str,
        json: Option<Value>,
        options: Map<String, Value>,
    ) -> Result<Vec<u8>, Error>;
    async fn close(&self) -> Result<(), Error>;
}

/// Clock port for testability.
pub trait ClockPort: Send + Sync {
    /// Return the current UTC datetime.
    fn now(&self) -> DateTime<Utc>;
}
