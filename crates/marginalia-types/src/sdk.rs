//! Plugin-boundary DTOs mirroring `research_engine_sdk/types.py`.
//!
//! The wire values are identical to core's, so the shared enums are re-used
//! rather than duplicated. `PassageDraft` is defined here because
//! `domain/passages.py` re-exports the SDK type.

use std::fmt;
use std::path::PathBuf;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

pub use crate::common::{DatePrecision, ExtractionStatus, FusionMode, MentionSource, NodeKind};
use crate::errors::{Error, Result};

/// Reference to a local source or URI.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SourceRef {
    #[serde(default)]
    pub path: Option<PathBuf>,
    #[serde(default)]
    pub uri: Option<String>,
    #[serde(default)]
    #[serde(with = "crate::wire::bytes_string_opt")]
    pub content_hash: Option<Vec<u8>>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

impl SourceRef {
    /// The `_has_one_location` model validator.
    pub fn validate(&self) -> Result<()> {
        if self.path.is_none() && self.uri.as_ref().is_none_or(String::is_empty) {
            return Err(Error::Validation(
                "SourceRef requires path or uri".to_owned(),
            ));
        }
        Ok(())
    }

    pub fn r#ref(&self) -> String {
        match &self.path {
            Some(p) => p.display().to_string(),
            None => self.uri.clone().unwrap_or_default(),
        }
    }

    pub fn is_local(&self) -> bool {
        self.path.is_some()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DetectionResult {
    pub confidence: f64,
    pub reason: String,
    #[serde(default = "default_true")]
    pub is_viable: bool,
}

fn default_true() -> bool {
    true
}

impl DetectionResult {
    /// Pydantic's `ge=0.0, le=1.0` bound on `confidence`.
    pub fn validate(&self) -> Result<()> {
        if !(0.0..=1.0).contains(&self.confidence) {
            return Err(Error::Validation(
                "confidence must be between 0 and 1".to_owned(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ParsedDocument {
    #[serde(default)]
    pub title: Option<String>,
    pub text: String,
    #[serde(default = "default_document_type")]
    pub document_type: String,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
    #[serde(default)]
    pub sections: Vec<Map<String, Value>>,
    #[serde(default)]
    pub structural_locators: Vec<Map<String, Value>>,
}

fn default_document_type() -> String {
    "generic".to_owned()
}

/// A chunk whose span addresses the canonical document text.
///
/// Core's `domain/passages.py` re-exports this exact type.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PassageDraft {
    #[serde(default)]
    pub position: i64,
    pub char_start: i64,
    pub char_end: i64,
    #[serde(default)]
    pub locator: Map<String, Value>,
    pub text: String,
    #[serde(default)]
    pub token_count: Option<i64>,
    pub chunker: String,
    pub chunker_version: String,
    #[serde(default)]
    pub metadata: Map<String, Value>,
    #[serde(default)]
    pub node_id: Option<Uuid>,
}

impl PassageDraft {
    /// The `_span_is_well_formed` model validator. Width is measured in
    /// Unicode scalar values — Python's `len(str)` counts the same units.
    pub fn validate(&self) -> Result<()> {
        if self.position < 0 {
            return Err(Error::Validation(
                "position is a 0-based order, never negative".to_owned(),
            ));
        }
        if self.char_start < 0 {
            return Err(Error::Validation(format!(
                "char_start must be non-negative, got {}",
                self.char_start
            )));
        }
        if self.char_end < self.char_start {
            return Err(Error::Validation(format!(
                "char_end ({}) precedes char_start ({})",
                self.char_end, self.char_start
            )));
        }
        let width = self.char_end - self.char_start;
        let len = self.text.chars().count() as i64;
        if width != len {
            return Err(Error::Validation(format!(
                "span width {width} does not match text length {len} — the span and the text disagree"
            )));
        }
        if let Some(t) = self.token_count {
            if t < 0 {
                return Err(Error::Validation(
                    "token_count must be non-negative".to_owned(),
                ));
            }
        }
        Ok(())
    }
}

/// A document-tree node before storage assigns an id.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NodeDraft {
    pub path: String,
    #[serde(default)]
    pub parent_path: Option<String>,
    #[serde(default)]
    pub depth: i64,
    #[serde(default)]
    pub position: i64,
    #[serde(default = "default_section")]
    pub node_type: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub char_start: i64,
    pub char_end: i64,
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

fn default_section() -> String {
    "section".to_owned()
}

impl NodeDraft {
    pub fn validate(&self) -> Result<()> {
        if self.depth < 0 || self.position < 0 || self.char_start < 0 {
            return Err(Error::Validation(
                "depth, position, and char_start must be non-negative".to_owned(),
            ));
        }
        if self.char_end < self.char_start {
            return Err(Error::Validation(format!(
                "char_end ({}) precedes char_start ({})",
                self.char_end, self.char_start
            )));
        }
        Ok(())
    }
}

#[derive(Clone, PartialEq, Serialize, Deserialize)]
pub struct PluginContext {
    pub plugin_id: String,
    pub data_dir: PathBuf,
    pub distribution_name: String,
    pub distribution_version: String,
    /// Database URL for plugin-owned migrations, if the loader was given
    /// one. Held as plaintext like `SecretStr.get_secret_value()` — serde
    /// emits the value, exactly as a python-mode dump does; only the JSON
    /// dump masks it, and no ported path dumps a context to JSON. Absent
    /// keys read as `None`, so pre-`database_url` payloads still parse.
    #[serde(default)]
    pub database_url: Option<String>,
}

/// `SecretStr` hides the value in `repr`; derived `Debug` would print it.
/// The shape mirrors the secret (`Some("**********")`, never the URL).
impl fmt::Debug for PluginContext {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PluginContext")
            .field("plugin_id", &self.plugin_id)
            .field("data_dir", &self.data_dir)
            .field("distribution_name", &self.distribution_name)
            .field("distribution_version", &self.distribution_version)
            .field(
                "database_url",
                &self.database_url.as_ref().map(|_| "**********"),
            )
            .finish()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FuzzyDate {
    pub start: DateTime<Utc>,
    pub end: DateTime<Utc>,
    pub precision: DatePrecision,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SdkDocument {
    pub id: Uuid,
    #[serde(default)]
    pub title: Option<String>,
    pub document_type: String,
    #[serde(default)]
    pub language: Option<String>,
    pub source: String,
    #[serde(with = "crate::wire::bytes_string")]
    pub content_hash: Vec<u8>,
    pub parser: String,
    pub parser_version: String,
    pub ingested_at: DateTime<Utc>,
    #[serde(default)]
    pub created_date_start: Option<DateTime<Utc>>,
    #[serde(default)]
    pub created_date_end: Option<DateTime<Utc>>,
    #[serde(default)]
    pub created_precision: Option<String>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SdkPassage {
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
    #[serde(default)]
    pub node_id: Option<Uuid>,
    #[serde(with = "crate::wire::bytes_string")]
    pub content_hash: Vec<u8>,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SdkEntity {
    pub id: Uuid,
    pub entity_type: String,
    pub canonical_name: String,
    #[serde(default)]
    pub disambiguator: Option<String>,
    #[serde(default)]
    pub attributes: Map<String, Value>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SdkMention {
    pub id: Uuid,
    pub passage_id: Uuid,
    pub entity_id: Uuid,
    #[serde(default)]
    pub span_start: Option<i64>,
    #[serde(default)]
    pub span_end: Option<i64>,
    pub surface_form: String,
    pub confidence: f64,
    pub source: MentionSource,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SdkEvent {
    pub id: Uuid,
    pub event_type: String,
    #[serde(default)]
    pub timestamp_start: Option<DateTime<Utc>>,
    #[serde(default)]
    pub timestamp_end: Option<DateTime<Utc>>,
    #[serde(default)]
    pub precision: Option<DatePrecision>,
    #[serde(default)]
    pub location_id: Option<Uuid>,
    #[serde(default)]
    pub location_text: Option<String>,
    #[serde(default)]
    pub source_passage_id: Option<Uuid>,
    #[serde(default)]
    pub payload: Map<String, Value>,
    #[serde(default = "default_confidence")]
    pub confidence: f64,
    pub created_at: DateTime<Utc>,
}

fn default_confidence() -> f64 {
    1.0
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct SdkEventFilter {
    #[serde(default)]
    pub event_types: Option<Vec<String>>,
    #[serde(default)]
    pub actor_entity_ids: Option<Vec<Uuid>>,
    #[serde(default)]
    pub date_range_start: Option<DateTime<Utc>>,
    #[serde(default)]
    pub date_range_end: Option<DateTime<Utc>>,
    #[serde(default)]
    pub location_id: Option<Uuid>,
    #[serde(default)]
    pub payload: Option<Map<String, Value>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SdkTimelineBucket {
    pub bucket: String,
    pub count: i64,
    #[serde(default)]
    pub aggregates: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SdkEdge {
    pub id: Uuid,
    pub source_kind: NodeKind,
    pub source_id: Uuid,
    pub target_kind: NodeKind,
    pub target_id: Uuid,
    pub relation_type: String,
    #[serde(default)]
    pub attributes: Map<String, Value>,
    #[serde(default)]
    pub source_passage_id: Option<Uuid>,
    #[serde(default = "default_confidence")]
    pub confidence: f64,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SdkExtractionRecord {
    pub id: Uuid,
    pub extraction_id: Uuid,
    pub passage_id: Uuid,
    pub schema_id: Uuid,
    pub record_type: String,
    pub data: Map<String, Value>,
    #[serde(default)]
    pub evidence_start: Option<i64>,
    #[serde(default)]
    pub evidence_end: Option<i64>,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct SdkSearchFilters {
    #[serde(default)]
    pub document_types: Option<Vec<String>>,
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
    #[serde(default = "default_and")]
    pub extension_logic: ExtensionLogic,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ExtensionLogic {
    #[default]
    And,
    Or,
}

fn default_and() -> ExtensionLogic {
    ExtensionLogic::And
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SdkSearchQuery {
    pub text: String,
    #[serde(default)]
    pub filters: Option<SdkSearchFilters>,
    #[serde(default = "default_k")]
    pub k: i64,
    #[serde(default = "default_k_vec")]
    pub k_vec: i64,
    #[serde(default = "default_k_kw")]
    pub k_kw: i64,
    #[serde(default)]
    pub fusion_mode: FusionMode,
    #[serde(default = "default_alpha")]
    pub alpha: f64,
    #[serde(default = "default_true_bool")]
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
fn default_true_bool() -> bool {
    true
}
fn default_rerank_n() -> i64 {
    30
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct SdkScoreBreakdown {
    #[serde(default)]
    pub vector: Option<f64>,
    #[serde(default)]
    pub keyword: Option<f64>,
    #[serde(default)]
    pub rerank: Option<f64>,
    #[serde(default)]
    pub rrf: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SdkPassageHit {
    pub passage_id: Uuid,
    pub document_id: Uuid,
    pub score: f64,
    #[serde(default)]
    pub score_breakdown: Option<SdkScoreBreakdown>,
    pub text: String,
    #[serde(default)]
    pub metadata: Map<String, Value>,
    #[serde(default)]
    pub locator: Map<String, Value>,
    #[serde(default)]
    pub char_start: Option<i64>,
    #[serde(default)]
    pub char_end: Option<i64>,
    #[serde(default)]
    pub node_id: Option<Uuid>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SdkSearchResult {
    pub hits: Vec<SdkPassageHit>,
    pub total_candidates: i64,
    #[serde(default)]
    pub applied_filters: Map<String, Value>,
    #[serde(default)]
    pub degraded: Vec<String>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Availability {
    InCorpus,
    Ingestable,
    Borrowable,
    Purchasable,
    #[default]
    ExternalOnly,
}

pub fn availability_rank(value: Availability) -> i64 {
    match value {
        Availability::InCorpus => 4,
        Availability::Ingestable => 3,
        Availability::Borrowable => 2,
        Availability::Purchasable => 1,
        Availability::ExternalOnly => 0,
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SourceQuery {
    pub query: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub author: Option<String>,
    #[serde(default)]
    pub year: Option<i64>,
    #[serde(default)]
    pub doi: Option<String>,
    #[serde(default)]
    pub isbn: Option<String>,
    #[serde(default)]
    pub asin: Option<String>,
    #[serde(default)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IngestAction {
    pub tool: String,
    #[serde(default)]
    pub args: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SourceMatch {
    pub plugin: String,
    pub source_id: String,
    pub title: String,
    #[serde(default)]
    pub authors: Vec<String>,
    #[serde(default)]
    pub year: Option<i64>,
    #[serde(default)]
    pub doi: Option<String>,
    #[serde(default)]
    pub isbn: Option<String>,
    #[serde(default)]
    pub availability: Availability,
    #[serde(default)]
    pub confidence: f64,
    #[serde(default)]
    pub ingest_action: Option<IngestAction>,
    #[serde(default)]
    pub document_id: Option<String>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

impl SourceMatch {
    /// Pydantic's `ge=0.0, le=1.0` bound on `confidence`.
    pub fn validate(&self) -> Result<()> {
        if !(0.0..=1.0).contains(&self.confidence) {
            return Err(Error::Validation(
                "confidence must be between 0 and 1".to_owned(),
            ));
        }
        Ok(())
    }
}
