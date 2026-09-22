//! Authored-works spine mirroring `domain/works.py`.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::errors::{Error, Result};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum WorkStatus {
    #[default]
    Draft,
    Published,
    Archived,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RevisionState {
    #[default]
    Draft,
    Frozen,
    Published,
    Superseded,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Placement {
    #[default]
    Inline,
    BlockEnd,
}

/// One row of `bibliography.editions`: an edition key seen at ingest.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Edition {
    pub id: Uuid,
    pub edition_key: String,
    #[serde(default)]
    pub csl: Map<String, Value>,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Work {
    pub id: Uuid,
    pub slug: String,
    pub title: String,
    pub work_type: String,
    #[serde(default)]
    pub status: WorkStatus,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default, rename = "abstract")]
    pub abstract_text: Option<String>,
    #[serde(default)]
    pub current_revision_id: Option<Uuid>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    #[serde(default)]
    pub archived_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkDraft {
    pub slug: String,
    pub title: String,
    pub work_type: String,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default, rename = "abstract")]
    pub abstract_text: Option<String>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

impl WorkDraft {
    pub fn validate(&self) -> Result<()> {
        if self.slug.trim().is_empty() || self.title.trim().is_empty() {
            return Err(Error::Validation("must be non-empty".to_owned()));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkRevision {
    pub id: Uuid,
    pub work_id: Uuid,
    pub revision_number: i64,
    #[serde(default)]
    pub parent_revision_id: Option<Uuid>,
    #[serde(default)]
    pub state: RevisionState,
    #[serde(default)]
    pub message: Option<String>,
    #[serde(default)]
    #[serde(with = "crate::wire::bytes_string_opt")]
    pub content_hash: Option<Vec<u8>>,
    #[serde(default = "default_created_by")]
    pub created_by: String,
    pub created_at: DateTime<Utc>,
    #[serde(default)]
    pub frozen_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub published_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

fn default_created_by() -> String {
    "user".to_owned()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkRevisionDraft {
    pub work_id: Uuid,
    #[serde(default = "default_revision_number")]
    pub revision_number: i64,
    #[serde(default)]
    pub parent_revision_id: Option<Uuid>,
    #[serde(default)]
    pub message: Option<String>,
    #[serde(default = "default_created_by")]
    pub created_by: String,
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

fn default_revision_number() -> i64 {
    1
}

impl WorkRevisionDraft {
    pub fn validate(&self) -> Result<()> {
        if self.revision_number < 1 {
            return Err(Error::Validation("revision_number starts at 1".to_owned()));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkBlock {
    pub id: Uuid,
    pub revision_id: Uuid,
    pub block_key: Uuid,
    #[serde(default)]
    pub parent_id: Option<Uuid>,
    pub position: i64,
    pub block_type: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub body_markdown: String,
    #[serde(default)]
    pub attributes: Map<String, Value>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkBlockDraft {
    pub revision_id: Uuid,
    pub block_key: Uuid,
    #[serde(default)]
    pub parent_id: Option<Uuid>,
    #[serde(default)]
    pub position: i64,
    pub block_type: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub body_markdown: String,
    #[serde(default)]
    pub attributes: Map<String, Value>,
}

impl WorkBlockDraft {
    pub fn validate(&self) -> Result<()> {
        if self.position < 0 {
            return Err(Error::Validation(
                "position is a 0-based order, never negative".to_owned(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BlockSourceLink {
    pub block_id: Uuid,
    pub source_span_id: Uuid,
    pub relation: String,
    #[serde(default)]
    pub confidence: Option<f64>,
    #[serde(default)]
    pub note: Option<String>,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlockSourceLinkDraft {
    pub block_id: Uuid,
    pub source_span_id: Uuid,
    pub relation: String,
    #[serde(default)]
    pub confidence: Option<f64>,
    #[serde(default)]
    pub note: Option<String>,
}

impl BlockSourceLinkDraft {
    pub fn validate(&self) -> Result<()> {
        if let Some(c) = self.confidence {
            check_fraction(c)?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BlockEntityLink {
    pub block_id: Uuid,
    pub entity_id: Uuid,
    pub relation: String,
    #[serde(default)]
    pub surface_form: Option<String>,
    pub created_at: DateTime<Utc>,
}

/// Both link kinds of one block, for get and trace.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct BlockLinks {
    #[serde(default)]
    pub sources: Vec<BlockSourceLink>,
    #[serde(default)]
    pub entities: Vec<BlockEntityLink>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlockEntityLinkDraft {
    pub block_id: Uuid,
    pub entity_id: Uuid,
    pub relation: String,
    #[serde(default)]
    pub surface_form: Option<String>,
}

/// A row that lets a gated finding pass: who, what, why.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Waiver {
    pub id: Uuid,
    pub revision_id: Uuid,
    pub rule_id: String,
    #[serde(default)]
    pub subject: Option<String>,
    pub actor: String,
    pub reason: String,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WaiverDraft {
    pub revision_id: Uuid,
    pub rule_id: String,
    #[serde(default)]
    pub subject: Option<String>,
    #[serde(default = "default_actor")]
    pub actor: String,
    pub reason: String,
}

fn default_actor() -> String {
    "user".to_owned()
}

impl WaiverDraft {
    pub fn validate(&self) -> Result<()> {
        if self.rule_id.trim().is_empty() || self.reason.trim().is_empty() {
            return Err(Error::Validation("must be non-empty".to_owned()));
        }
        if self.actor.trim().is_empty() {
            return Err(Error::Validation(
                "a waiver records who, not just why".to_owned(),
            ));
        }
        Ok(())
    }
}

fn check_fraction(value: f64) -> Result<()> {
    if !(0.0..=1.0).contains(&value) {
        return Err(Error::Validation(
            "confidence must be between 0 and 1".to_owned(),
        ));
    }
    Ok(())
}
