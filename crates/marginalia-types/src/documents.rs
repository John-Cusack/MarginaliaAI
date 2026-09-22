//! Document types mirroring `domain/documents.py`.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

/// A fully ingested document.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Document {
    pub id: Uuid,
    pub title: Option<String>,
    pub document_type: String,
    pub language: Option<String>,
    pub source: String,
    #[serde(with = "crate::wire::bytes_string")]
    pub content_hash: Vec<u8>,
    pub parser: String,
    pub parser_version: String,
    pub ingested_at: DateTime<Utc>,
    pub created_date_start: Option<DateTime<Utc>>,
    pub created_date_end: Option<DateTime<Utc>>,
    pub created_precision: Option<String>,
    /// The edition this document instantiates, if edition-bound.
    /// Position mirrors the Python model (before `metadata`) so serialized
    /// key order stays identical; absent keys read as `None`.
    #[serde(default)]
    pub edition_id: Option<Uuid>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

/// Data needed to create a document record.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DocumentDraft {
    pub title: Option<String>,
    #[serde(default = "default_document_type")]
    pub document_type: String,
    pub language: Option<String>,
    pub source: String,
    #[serde(with = "crate::wire::bytes_string")]
    pub content_hash: Vec<u8>,
    pub parser: String,
    pub parser_version: String,
    #[serde(default)]
    pub created_date_start: Option<DateTime<Utc>>,
    #[serde(default)]
    pub created_date_end: Option<DateTime<Utc>>,
    #[serde(default)]
    pub created_precision: Option<String>,
    /// The edition the draft instantiates, if edition-bound (same position
    /// and back-compat rule as [`Document::edition_id`]).
    #[serde(default)]
    pub edition_id: Option<Uuid>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

fn default_document_type() -> String {
    "generic".to_owned()
}

/// A document's canonical text — the substrate passage offsets address.
///
/// `text` is authoritative: passage `char_start` / `char_end` index into it.
/// `normalized_text` is a lossy fold for quote matching and must never be
/// used for addressing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DocumentText {
    pub document_id: Uuid,
    pub text: String,
    pub normalized_text: String,
    pub normalization_version: String,
    pub parser: String,
    pub parser_version: String,
}

/// Filters for querying documents.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct DocumentFilter {
    #[serde(default)]
    pub document_types: Option<Vec<String>>,
    #[serde(default)]
    pub date_start: Option<DateTime<Utc>>,
    #[serde(default)]
    pub date_end: Option<DateTime<Utc>>,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default)]
    pub source_pattern: Option<String>,
    #[serde(default)]
    pub metadata: Option<Map<String, Value>>,
}
