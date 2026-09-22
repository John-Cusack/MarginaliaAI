//! Cited addresses mirroring `domain/spans.py`.
//!
//! A span owns its address and its canonical slice. The citing row owns the
//! typed quote, the tier, and the timestamp — two citers share one span while
//! disagreeing about wording.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// One row of `evidence.source_spans`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceSpan {
    pub id: Uuid,
    pub document_id: Uuid,
    pub char_start: i64,
    pub char_end: i64,
    /// `document_texts.text[char_start:char_end]`, written by the resolver.
    /// Never the citer's typed quote.
    pub quoted_text: String,
    pub parser: Option<String>,
    pub parser_version: Option<String>,
    /// Best-overlap passage, a cache only. Null when no passage overlaps.
    pub passage_id: Option<Uuid>,
    pub created_at: DateTime<Utc>,
}
