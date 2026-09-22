//! Shared primitives mirroring `domain/common.py`.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

pub type EntityId = Uuid;
pub type DocumentId = Uuid;
pub type PassageId = Uuid;
pub type EventId = Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum NodeKind {
    Entity,
    Document,
    Passage,
    Event,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DatePrecision {
    Day,
    Week,
    Month,
    Season,
    Year,
    Decade,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ExtractionStatus {
    Pending,
    Ok,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum IngestionRunStatus {
    Running,
    Ok,
    Failed,
    Partial,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum IngestionItemStatus {
    Pending,
    Ok,
    Failed,
    Skipped,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MentionSource {
    LlmExtraction,
    Rule,
    Manual,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FusionMode {
    #[default]
    Rrf,
    Weighted,
    VectorOnly,
    KeywordOnly,
}
