//! Edge/relationship types mirroring `domain/edges.py`.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::common::NodeKind;

/// A directed edge between two nodes.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Edge {
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

fn default_confidence() -> f64 {
    1.0
}

/// Data needed to create an edge.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EdgeDraft {
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
}
