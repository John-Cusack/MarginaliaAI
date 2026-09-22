//! Extraction framework types mirroring `domain/extractions.py`.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::common::ExtractionStatus;

/// A registered extraction schema.
///
/// `schema_def` serializes as `schema` on the wire (`alias="schema"`,
/// `populate_by_name` in Python).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExtractionSchema {
    pub id: Uuid,
    pub name: String,
    pub version: i64,
    pub owner: String,
    /// Accepts `schema_def` too: Python's default dump uses the field name.
    #[serde(rename = "schema", alias = "schema_def")]
    pub schema_def: Map<String, Value>,
    pub prompt_template: String,
    pub created_at: DateTime<Utc>,
}

/// Data needed to register an extraction schema.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExtractionSchemaDraft {
    pub name: String,
    pub version: i64,
    pub owner: String,
    pub schema_def: Map<String, Value>,
    pub prompt_template: String,
}

/// One row per (passage x schema x extractor_version) invocation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Extraction {
    pub id: Uuid,
    pub passage_id: Uuid,
    pub schema_id: Uuid,
    pub extractor_version: String,
    pub llm_model: String,
    pub status: ExtractionStatus,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub records: Vec<Map<String, Value>>,
    #[serde(default)]
    pub llm_call_id: Option<Uuid>,
    pub created_at: DateTime<Utc>,
}

/// A single extracted record, materialized for queryability.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExtractionRecord {
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

/// Options for running an extraction.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExtractionOptions {
    #[serde(default)]
    pub force_refresh: bool,
    #[serde(default)]
    pub llm_model: Option<String>,
    #[serde(default = "default_concurrency")]
    pub concurrency: i64,
    #[serde(default = "default_batch_size")]
    pub batch_size: i64,
    #[serde(default = "default_true")]
    pub retry_on_validation_error: bool,
    #[serde(default = "default_caller")]
    pub caller: String,
}

fn default_concurrency() -> i64 {
    8
}
fn default_batch_size() -> i64 {
    10
}
fn default_true() -> bool {
    true
}
fn default_caller() -> String {
    "core".to_owned()
}

/// Result of extracting from a single passage.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExtractionResult {
    pub passage_id: Uuid,
    pub status: ExtractionStatus,
    #[serde(default)]
    pub records: Vec<Map<String, Value>>,
    #[serde(default)]
    pub from_cache: bool,
    #[serde(default)]
    pub llm_call_id: Option<Uuid>,
    #[serde(default)]
    pub error: Option<String>,
}

impl ExtractionResult {
    /// The `from_cached` constructor.
    pub fn from_cached(extraction: &Extraction) -> Self {
        Self {
            passage_id: extraction.passage_id,
            status: extraction.status,
            records: extraction.records.clone(),
            from_cache: true,
            llm_call_id: extraction.llm_call_id,
            error: None,
        }
    }
}

/// Result of a batch extraction.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExtractionBatch {
    pub results: Vec<ExtractionResult>,
    pub schema_name: String,
    pub schema_version: i64,
}
