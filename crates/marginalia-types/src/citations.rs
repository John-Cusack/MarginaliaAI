//! Citation occurrences and items mirroring `domain/citations.py`.
//!
//! An occurrence is the `{{cite:<key>}}` marker's row: which block, which
//! intent, which placement. An item is one grounding of that marker.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::errors::{Error, Result};
use crate::works::Placement;
use crate::works_files::Intent;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CitationOccurrence {
    pub id: Uuid,
    pub citation_key: Uuid,
    pub block_id: Uuid,
    #[serde(default)]
    pub placement: Placement,
    #[serde(default)]
    pub intent: Intent,
    #[serde(default)]
    pub note: Option<String>,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OccurrenceDraft {
    pub block_id: Uuid,
    pub citation_key: Uuid,
    #[serde(default)]
    pub placement: Placement,
    #[serde(default)]
    pub intent: Intent,
    #[serde(default)]
    pub note: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CitationItem {
    pub occurrence_id: Uuid,
    pub position: i64,
    #[serde(default)]
    pub edition_id: Option<Uuid>,
    #[serde(default)]
    pub edition_key: Option<String>,
    #[serde(default)]
    pub source_span_id: Option<Uuid>,
    #[serde(default)]
    pub quoted_text: Option<String>,
    #[serde(default)]
    pub verify_status: Option<String>,
    #[serde(default)]
    pub verified_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub locator: Map<String, Value>,
    #[serde(default)]
    pub prefix: Option<String>,
    #[serde(default)]
    pub suffix: Option<String>,
    #[serde(default)]
    pub suppress_author: bool,
}

/// An item validates before the database does: identity first, then the quote.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CitationItemDraft {
    pub occurrence_id: Uuid,
    #[serde(default)]
    pub position: i64,
    #[serde(default)]
    pub edition_id: Option<Uuid>,
    #[serde(default)]
    pub edition_key: Option<String>,
    #[serde(default)]
    pub source_span_id: Option<Uuid>,
    #[serde(default)]
    pub quoted_text: Option<String>,
    #[serde(default)]
    pub verify_status: Option<String>,
    #[serde(default)]
    pub locator: Map<String, Value>,
    #[serde(default)]
    pub prefix: Option<String>,
    #[serde(default)]
    pub suffix: Option<String>,
    #[serde(default)]
    pub suppress_author: bool,
}

impl CitationItemDraft {
    /// The `_names_an_edition` and `_quote_needs_a_span` model validators.
    pub fn validate(&self) -> Result<()> {
        if self.edition_id.is_none() && self.edition_key.is_none() {
            return Err(Error::Validation(
                "an item names its edition: edition_id or edition_key".to_owned(),
            ));
        }
        if self.quoted_text.is_some() && self.source_span_id.is_none() {
            return Err(Error::Validation(
                "quoted_text without source_span_id is unaddressed".to_owned(),
            ));
        }
        Ok(())
    }
}

/// One occurrence with everything grounding it, for export and validate.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BlockCitations {
    pub occurrence: CitationOccurrence,
    #[serde(default)]
    pub items: Vec<CitationItem>,
}
