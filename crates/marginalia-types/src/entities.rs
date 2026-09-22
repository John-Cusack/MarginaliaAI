//! Entity and mention types mirroring `domain/entities.py`.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::common::MentionSource;

/// A canonical entity (person, place, org, etc.).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Entity {
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

/// Data needed to create an entity.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EntityDraft {
    pub entity_type: String,
    pub canonical_name: String,
    #[serde(default)]
    pub disambiguator: Option<String>,
    #[serde(default)]
    pub attributes: Map<String, Value>,
}

/// An alias for an entity.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EntityAlias {
    pub entity_id: Uuid,
    pub alias: String,
    #[serde(default)]
    pub alias_type: Option<String>,
}

/// A mention of an entity in a passage.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Mention {
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

/// Data needed to create a mention record.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MentionDraft {
    pub passage_id: Uuid,
    pub entity_id: Uuid,
    #[serde(default)]
    pub span_start: Option<i64>,
    #[serde(default)]
    pub span_end: Option<i64>,
    pub surface_form: String,
    pub confidence: f64,
    pub source: MentionSource,
}

/// A candidate entity from resolution.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EntityCandidate {
    pub entity_id: Uuid,
    pub canonical_name: String,
    pub entity_type: String,
    #[serde(default)]
    pub disambiguator: Option<String>,
    #[serde(default)]
    pub aliases: Vec<String>,
    #[serde(default)]
    pub attributes: Map<String, Value>,
    pub match_score: f64,
}
