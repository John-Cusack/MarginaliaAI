//! Plugin-boundary DTOs the shipped seams produce, mirroring
//! `research_engine_sdk/types.py`: `ParsedDocument` from the parser and
//! `PassageDraft` from the chunkers.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::errors::{Error, Result};

/// A parsed source document.
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
