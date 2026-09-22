//! Passage and search types mirroring `domain/passages.py`.
//!
//! `PassageDraft` is defined in [`crate::sdk`] — core re-exports the SDK type
//! exactly as `domain/passages.py` re-exports `research_engine_sdk`'s — and is
//! re-exported here for the same reason.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

pub use crate::sdk::PassageDraft;

/// A chunk of a document — the unit of retrieval.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Passage {
    pub id: Uuid,
    pub document_id: Uuid,
    pub position: i64,
    #[serde(default)]
    pub char_start: Option<i64>,
    #[serde(default)]
    pub char_end: Option<i64>,
    #[serde(default)]
    pub locator: Map<String, Value>,
    pub text: String,
    #[serde(default)]
    pub token_count: Option<i64>,
    pub chunker: String,
    pub chunker_version: String,
    #[serde(default)]
    pub metadata: Map<String, Value>,
    /// The structural node containing this passage. None for corpora ingested
    /// before nodes existed.
    #[serde(default)]
    pub node_id: Option<Uuid>,
    #[serde(with = "crate::wire::bytes_string")]
    pub content_hash: Vec<u8>,
    pub created_at: DateTime<Utc>,
}

/// The expanded read of a hit — more than matched, bounded by structure.
///
/// Keeps the `PassageDraft` invariant `text == canonical[char_start:char_end]`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PassageWindow {
    pub text: String,
    pub char_start: i64,
    pub char_end: i64,
    /// `node` means the window *is* one structural node; anything else means
    /// it is a slice and reading the node would give more.
    pub source: WindowSource,
    /// The node that bounded the window — not always the passage's own node.
    #[serde(default)]
    pub node_id: Option<Uuid>,
    /// Ancestor titles, root first. The citation for this window.
    #[serde(default)]
    pub breadcrumb: Vec<String>,
    /// Measured on the returned text, not on the estimate that sized it.
    pub approx_tokens: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WindowSource {
    Node,
    NodeWindow,
    DocumentWindow,
    Passage,
}

/// What a hit is cited from — the citation draft for a search result.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HitSource {
    #[serde(default)]
    pub document_title: Option<String>,
    /// The bibliographic join, once a pack writes it at ingest. None until then.
    #[serde(default)]
    pub edition_key: Option<String>,
    /// `documents.metadata.edition`, when a pack wrote one.
    #[serde(default)]
    pub edition: Option<String>,
    /// `document_texts.parser_version`. None when there is no canonical text.
    #[serde(default)]
    pub parser_version: Option<String>,
    #[serde(default)]
    pub has_canonical_text: bool,
    /// False on rows from older chunkers lacking offsets: not citable.
    #[serde(default)]
    pub has_offsets: bool,
}

/// A passage returned by search with scores.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PassageHit {
    pub passage_id: Uuid,
    pub document_id: Uuid,
    pub score: f64,
    #[serde(default)]
    pub score_breakdown: Option<ScoreBreakdown>,
    #[serde(default)]
    pub source: Option<HitSource>,
    /// The chunk that actually matched — what was embedded, ranked, reranked.
    pub text: String,
    #[serde(default)]
    pub metadata: Map<String, Value>,
    #[serde(default)]
    pub locator: Map<String, Value>,
    /// Carried so a caller can widen a hit without re-reading the row.
    #[serde(default)]
    pub char_start: Option<i64>,
    #[serde(default)]
    pub char_end: Option<i64>,
    #[serde(default)]
    pub node_id: Option<Uuid>,
    /// None when the document has no canonical text, or the passage no offsets.
    #[serde(default)]
    pub window: Option<PassageWindow>,
}

/// Breakdown of how the score was computed.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ScoreBreakdown {
    #[serde(default)]
    pub vector: Option<f64>,
    #[serde(default)]
    pub keyword: Option<f64>,
    #[serde(default)]
    pub rerank: Option<f64>,
    #[serde(default)]
    pub rrf: Option<f64>,
}

/// Search query with filters and hybrid options.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SearchQuery {
    pub text: String,
    #[serde(default)]
    pub filters: Option<SearchFilters>,
    #[serde(default = "default_k")]
    pub k: i64,
    #[serde(default = "default_k_vec")]
    pub k_vec: i64,
    #[serde(default = "default_k_kw")]
    pub k_kw: i64,
    #[serde(default)]
    pub fusion_mode: crate::common::FusionMode,
    #[serde(default = "default_alpha")]
    pub alpha: f64,
    #[serde(default = "default_true")]
    pub rerank: bool,
    #[serde(default = "default_rerank_n")]
    pub rerank_n: i64,
}

fn default_k() -> i64 {
    20
}
fn default_k_vec() -> i64 {
    100
}
fn default_k_kw() -> i64 {
    100
}
fn default_alpha() -> f64 {
    0.5
}
fn default_true() -> bool {
    true
}

fn default_rerank_n() -> i64 {
    30
}

/// Filters for narrowing search results.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct SearchFilters {
    #[serde(default)]
    pub document_types: Option<Vec<String>>,
    /// Raw strings in core (parsed downstream), unlike the SDK's datetimes.
    #[serde(default)]
    pub date_range_start: Option<String>,
    #[serde(default)]
    pub date_range_end: Option<String>,
    #[serde(default)]
    pub author_entity_id: Option<Uuid>,
    #[serde(default)]
    pub recipient_entity_id: Option<Uuid>,
    #[serde(default)]
    pub mentions_entity_ids: Option<Vec<Uuid>>,
    #[serde(default)]
    pub metadata: Option<Map<String, Value>>,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default)]
    pub extensions: Option<Map<String, Value>>,
    #[serde(default = "default_extension_logic")]
    pub extension_logic: ExtensionLogic,
}

fn default_extension_logic() -> ExtensionLogic {
    ExtensionLogic::And
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ExtensionLogic {
    #[default]
    And,
    Or,
}

/// Result of a search operation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SearchResult {
    pub hits: Vec<PassageHit>,
    pub total_candidates: i64,
    #[serde(default)]
    pub applied_filters: Map<String, Value>,
    #[serde(default)]
    pub degraded: Vec<String>,
}
