//! Provenance and operations types mirroring `domain/provenance.py`.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::common::{IngestionItemStatus, IngestionRunStatus};
use crate::errors::{Error, Result};

/// A logged LLM call for auditability and cost tracking.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LlmCall {
    pub id: Uuid,
    pub purpose: String,
    pub caller: String,
    pub model: String,
    #[serde(default)]
    pub input_tokens: Option<i64>,
    #[serde(default)]
    pub output_tokens: Option<i64>,
    #[serde(default)]
    pub cost_estimate: Option<f64>,
    #[serde(default)]
    pub duration_ms: Option<i64>,
    pub status: String,
    #[serde(default)]
    pub error: Option<String>,
    pub created_at: DateTime<Utc>,
}

/// Data needed to log an LLM call.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LlmCallDraft {
    pub purpose: String,
    pub caller: String,
    pub model: String,
    #[serde(default)]
    pub input_tokens: Option<i64>,
    #[serde(default)]
    pub output_tokens: Option<i64>,
    #[serde(default)]
    pub cost_estimate: Option<f64>,
    #[serde(default)]
    pub duration_ms: Option<i64>,
    pub status: String,
    #[serde(default)]
    pub error: Option<String>,
}

/// Spend and token counts for one combination of grouping keys.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct UsageGroup {
    pub key: std::collections::BTreeMap<String, String>,
    pub calls: i64,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cost: f64,
    pub errors: i64,
}

/// Aggregated LLM spend over a time window.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct UsageSummary {
    #[serde(default)]
    pub since: Option<DateTime<Utc>>,
    #[serde(default)]
    pub until: Option<DateTime<Utc>>,
    pub group_by: Vec<String>,
    pub groups: Vec<UsageGroup>,
    pub total_calls: i64,
    pub total_cost: f64,
}

/// A batch ingestion run record.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IngestionRun {
    pub id: Uuid,
    pub started_at: DateTime<Utc>,
    #[serde(default)]
    pub completed_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub source_spec: Map<String, Value>,
    pub status: IngestionRunStatus,
    #[serde(default)]
    pub stats: Map<String, Value>,
}

/// A single item within an ingestion run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IngestionItem {
    pub id: Uuid,
    pub run_id: Uuid,
    pub source_ref: String,
    #[serde(default)]
    pub document_id: Option<Uuid>,
    pub status: IngestionItemStatus,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub duration_ms: Option<i64>,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PluginActivationState {
    Available,
    Enabled,
    Disabled,
    PendingApproval,
    Missing,
    Incompatible,
    Error,
    #[default]
    Legacy,
}

/// Approval and runtime state for one installed plugin distribution.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PluginActivation {
    pub plugin_id: String,
    #[serde(default)]
    pub distribution_name: Option<String>,
    pub distribution_version: String,
    #[serde(default)]
    pub entry_point_name: Option<String>,
    #[serde(default)]
    pub manifest_sha256: Option<String>,
    pub manifest: Map<String, Value>,
    pub permissions_granted: Map<String, Value>,
    pub installed_at: DateTime<Utc>,
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub state: PluginActivationState,
    #[serde(default)]
    pub approved_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub approved_non_interactive: bool,
    #[serde(default)]
    pub last_seen_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub last_error: Option<String>,
    #[serde(default)]
    pub provenance: Option<Map<String, Value>>,
    #[serde(default)]
    pub legacy_source_url: Option<String>,
    #[serde(default)]
    pub legacy_source_ref: Option<String>,
    #[serde(default)]
    pub database_revision: Option<i64>,
    #[serde(default)]
    pub database_status: Option<String>,
}

impl PluginActivation {
    /// Non-legacy rows carry full distribution identity.
    pub fn validate(&self) -> Result<()> {
        if self.state == PluginActivationState::Legacy {
            return Ok(());
        }
        let mut missing = Vec::new();
        if self.distribution_name.is_none() {
            missing.push("distribution_name");
        }
        if self.entry_point_name.is_none() {
            missing.push("entry_point_name");
        }
        if self.manifest_sha256.is_none() {
            missing.push("manifest_sha256");
        }
        if self.approved_at.is_none() {
            missing.push("approved_at");
        }
        if missing.is_empty() {
            Ok(())
        } else {
            Err(Error::Validation(format!(
                "non-legacy plugin activation requires {}",
                missing.join(", ")
            )))
        }
    }
}

/// The `BudgetExceeded` exception payload: spend, limit, and window.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BudgetExceeded {
    pub spent: f64,
    pub limit: f64,
    pub window_days: i64,
}

impl BudgetExceeded {
    pub fn message(&self) -> String {
        format!(
            "LLM budget exceeded: ${:.2} spent in the last {}d against a ${:.2} limit.",
            self.spent, self.window_days, self.limit
        )
    }
}
