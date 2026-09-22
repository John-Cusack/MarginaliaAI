//! Event and timeline types mirroring `domain/events.py`.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::common::DatePrecision;

/// A date with explicit precision.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FuzzyDate {
    pub start: DateTime<Utc>,
    pub end: DateTime<Utc>,
    pub precision: DatePrecision,
}

/// An event in the timeline.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Event {
    pub id: Uuid,
    pub event_type: String,
    #[serde(default)]
    pub timestamp_start: Option<DateTime<Utc>>,
    #[serde(default)]
    pub timestamp_end: Option<DateTime<Utc>>,
    #[serde(default)]
    pub precision: Option<DatePrecision>,
    #[serde(default)]
    pub location_id: Option<Uuid>,
    #[serde(default)]
    pub location_text: Option<String>,
    #[serde(default)]
    pub source_passage_id: Option<Uuid>,
    #[serde(default)]
    pub payload: Map<String, Value>,
    #[serde(default = "default_confidence")]
    pub confidence: f64,
    pub created_at: DateTime<Utc>,
}

fn default_confidence() -> f64 {
    1.0
}

/// Data needed to create an event.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EventDraft {
    pub event_type: String,
    #[serde(default)]
    pub timestamp_start: Option<DateTime<Utc>>,
    #[serde(default)]
    pub timestamp_end: Option<DateTime<Utc>>,
    #[serde(default)]
    pub precision: Option<DatePrecision>,
    #[serde(default)]
    pub location_id: Option<Uuid>,
    #[serde(default)]
    pub location_text: Option<String>,
    #[serde(default)]
    pub source_passage_id: Option<Uuid>,
    #[serde(default)]
    pub payload: Map<String, Value>,
    #[serde(default = "default_confidence")]
    pub confidence: f64,
}

/// An entity participating in an event.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EventActor {
    pub event_id: Uuid,
    pub entity_id: Uuid,
    pub role: String,
}

/// Filters for querying events.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct EventFilter {
    #[serde(default)]
    pub event_types: Option<Vec<String>>,
    #[serde(default)]
    pub actor_entity_ids: Option<Vec<Uuid>>,
    #[serde(default)]
    pub date_range_start: Option<DateTime<Utc>>,
    #[serde(default)]
    pub date_range_end: Option<DateTime<Utc>>,
    #[serde(default)]
    pub location_id: Option<Uuid>,
    #[serde(default)]
    pub payload: Option<Map<String, Value>>,
}

/// A time bucket in a timeline aggregation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TimelineBucket {
    pub bucket: String,
    pub count: i64,
    #[serde(default)]
    pub aggregates: Map<String, Value>,
}

/// A named stream in a timeline comparison.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TimelineStream {
    pub name: String,
    pub buckets: Vec<TimelineBucket>,
}
