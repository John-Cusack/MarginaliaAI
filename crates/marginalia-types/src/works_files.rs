//! Work files mirroring `domain/works_files.py`: markdown works with
//! machine-checked citations.

use chrono::NaiveDate;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::errors::{Error, Result};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Intent {
    Quotation,
    Translation,
    Support,
    Contrast,
    Background,
    Definition,
    #[default]
    Source,
    SeeAlso,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Asserts,
    Supports,
    Rebuts,
    Context,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum WorkType {
    Translation,
    Essay,
    Dossier,
    Script,
    Outline,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum WorkFileStatus {
    Draft,
    Review,
    Published,
}

/// Citation ids are `[^cN]` handles: `c` followed by digits, nothing else.
pub fn is_entry_id(value: &str) -> bool {
    let mut chars = value.chars();
    if chars.next() != Some('c') {
        return false;
    }
    let rest = chars.as_str();
    !rest.is_empty() && rest.bytes().all(|b| b.is_ascii_digit())
}

/// One structured span citation from a work's front matter.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CitationEntry {
    pub id: String,
    pub intent: Intent,
    #[serde(default)]
    pub role: Option<Role>,
    pub document_id: Uuid,
    pub char_start: i64,
    pub char_end: i64,
    pub quoted_text: String,
    #[serde(default)]
    pub edition: Option<String>,
    #[serde(default)]
    pub edition_key: Option<String>,
    #[serde(default)]
    pub locator: Map<String, Value>,
}

impl CitationEntry {
    /// The `_id_is_a_handle`, `_start_is_an_offset`, `_quote_is_non_empty`,
    /// and `_span_is_well_formed` validators.
    pub fn validate(&self) -> Result<()> {
        if !is_entry_id(&self.id) {
            return Err(Error::Validation(format!(
                "id must match ^c[0-9]+$, got {:?}",
                self.id
            )));
        }
        if self.char_start < 0 {
            return Err(Error::Validation(format!(
                "char_start must be non-negative, got {}",
                self.char_start
            )));
        }
        if self.quoted_text.trim().is_empty() {
            return Err(Error::Validation(
                "quoted_text must be non-empty".to_owned(),
            ));
        }
        if self.char_end <= self.char_start {
            return Err(Error::Validation(format!(
                "char_end ({}) must exceed char_start ({})",
                self.char_end, self.char_start
            )));
        }
        Ok(())
    }
}

/// The validated header of a work file. Entries that failed validation live in
/// `WorkFile.entry_errors`, so one bad entry does not hide the others.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkFrontMatter {
    pub work: String,
    pub title: String,
    #[serde(rename = "type")]
    pub work_type: WorkType,
    pub status: WorkFileStatus,
    pub created: NaiveDate,
    #[serde(default)]
    pub claims: Vec<String>,
    #[serde(default)]
    pub citations: Vec<CitationEntry>,
}

/// One front-matter entry that failed validation, with the reason.
///
/// Not an error: the file still parses and `work_verify` reports it as
/// `AUTH_ENTRY_INVALID`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EntryError {
    #[serde(default)]
    pub citation_id: Option<String>,
    pub message: String,
}

/// A parsed work file: header models, body text, and marker handles.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkFile {
    pub work_path: String,
    pub front_matter: WorkFrontMatter,
    /// sha256 hex of the raw YAML block bytes, fences excluded.
    pub front_matter_sha: String,
    pub body: String,
    /// Every `[^cN]` in the body, in order, excluding footnote definitions.
    #[serde(default)]
    pub markers: Vec<String>,
    #[serde(default)]
    pub entry_errors: Vec<EntryError>,
}
