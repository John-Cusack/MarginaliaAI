//! Claims, anchors, and argument-graph edges mirroring `domain/claims.py`.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::errors::{Error, Result};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ClaimKind {
    Opposition,
    Mine,
    Premise,
    Lexical,
    Ally,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ClaimStatus {
    #[default]
    Open,
    Researching,
    Rebutted,
    Weakened,
    Unresolved,
    Conceded,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ClaimRelation {
    DependsOn,
    Supports,
    Contradicts,
    Refines,
    Rebuts,
    Concedes,
    Entails,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AnchorRole {
    Asserts,
    Supports,
    Rebuts,
    Context,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AnchorVerifyStatus {
    Exact,
    Normalized,
    Near,
}

/// Green means the implemented mechanical checks found no failure. It does not
/// mean the argument is sound or the source has been interpreted faithfully.
pub const CLAIM_AUDIT_ASSURANCE: &str = "Green means the implemented mechanical checks found no failure. It does not mean the argument is sound or the source has been interpreted faithfully.";

/// One addressable proposition in `argument.claims`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Claim {
    pub id: Uuid,
    pub r#ref: String,
    pub statement: String,
    pub kind: ClaimKind,
    pub status: ClaimStatus,
    #[serde(default)]
    pub confidence: Option<f64>,
    #[serde(default)]
    pub steelman: Option<String>,
    #[serde(default)]
    pub public_ready: bool,
    #[serde(default)]
    pub academic_candidate: bool,
    #[serde(default)]
    pub attributes: Map<String, Value>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

/// Mutable claim fields accepted by the ledger write path.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClaimDraft {
    pub r#ref: String,
    pub statement: String,
    pub kind: ClaimKind,
    #[serde(default)]
    pub status: ClaimStatus,
    #[serde(default)]
    pub confidence: Option<f64>,
    #[serde(default)]
    pub steelman: Option<String>,
    #[serde(default)]
    pub public_ready: bool,
    #[serde(default)]
    pub academic_candidate: bool,
    #[serde(default)]
    pub attributes: Map<String, Value>,
}

impl ClaimDraft {
    /// Non-empty ref/statement plus the 0..=1 confidence bound.
    pub fn validate(&self) -> Result<()> {
        if self.r#ref.trim().is_empty() || self.statement.trim().is_empty() {
            return Err(Error::Validation("must be non-empty".to_owned()));
        }
        if let Some(c) = self.confidence {
            check_fraction(c)?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClaimEdge {
    pub id: Uuid,
    pub source_id: Uuid,
    pub target_id: Uuid,
    pub relation: ClaimRelation,
    #[serde(default)]
    pub confidence: Option<f64>,
    #[serde(default)]
    pub note: Option<String>,
    pub created_at: DateTime<Utc>,
}

/// An outgoing edge named by the target's stable claim ref.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClaimEdgeDraft {
    pub target_ref: String,
    pub relation: ClaimRelation,
    #[serde(default)]
    pub confidence: Option<f64>,
    #[serde(default)]
    pub note: Option<String>,
}

impl ClaimEdgeDraft {
    pub fn validate(&self) -> Result<()> {
        if self.target_ref.trim().is_empty() {
            return Err(Error::Validation("must be non-empty".to_owned()));
        }
        if let Some(c) = self.confidence {
            check_fraction(c)?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Anchor {
    pub id: Uuid,
    pub claim_id: Uuid,
    pub role: AnchorRole,
    #[serde(default)]
    pub person_entity_id: Option<Uuid>,
    pub source_span_id: Uuid,
    pub quoted_text: String,
    #[serde(default)]
    pub verify_status: Option<AnchorVerifyStatus>,
    #[serde(default)]
    pub verified_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub parser_version: Option<String>,
    #[serde(default)]
    pub edition_id: Option<Uuid>,
    #[serde(default)]
    pub edition_key: Option<String>,
    #[serde(default)]
    pub locator: Map<String, Value>,
    pub created_at: DateTime<Utc>,
}

/// A verified anchor ready for insertion.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnchorDraft {
    pub role: AnchorRole,
    pub quoted_text: String,
    pub source_span_id: Uuid,
    #[serde(default)]
    pub person_entity_id: Option<Uuid>,
    pub verify_status: AnchorVerifyStatus,
    pub verified_at: DateTime<Utc>,
    #[serde(default)]
    pub parser_version: Option<String>,
    #[serde(default)]
    pub edition_id: Option<Uuid>,
    #[serde(default)]
    pub edition_key: Option<String>,
    #[serde(default)]
    pub locator: Map<String, Value>,
}

impl AnchorDraft {
    /// Non-empty quote plus the asserts-names-a-person rule.
    pub fn validate(&self) -> Result<()> {
        if self.quoted_text.trim().is_empty() {
            return Err(Error::Validation("must be non-empty".to_owned()));
        }
        if self.role == AnchorRole::Asserts && self.person_entity_id.is_none() {
            return Err(Error::Validation(
                "an asserts anchor must name a person".to_owned(),
            ));
        }
        Ok(())
    }
}

/// Caller input before quote verification and span resolution.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnchorInput {
    pub role: AnchorRole,
    pub quote: String,
    pub document_id: Uuid,
    #[serde(default)]
    pub person: Option<String>,
    #[serde(default)]
    pub edition_key: Option<String>,
    #[serde(default)]
    pub locator: Map<String, Value>,
}

impl AnchorInput {
    pub fn validate(&self) -> Result<()> {
        if self.quote.trim().is_empty() {
            return Err(Error::Validation("must be non-empty".to_owned()));
        }
        if self.role == AnchorRole::Asserts
            && self.person.as_ref().is_none_or(|p| p.trim().is_empty())
        {
            return Err(Error::Validation(
                "an asserts anchor must name a person".to_owned(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClaimFinding {
    pub rule_id: String,
    pub severity: Severity,
    pub claim_ref: String,
    pub message: String,
    #[serde(default)]
    pub detail: Option<Map<String, Value>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Error,
    Warning,
    Info,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClaimAuditReport {
    #[serde(default)]
    pub findings: Vec<ClaimFinding>,
    #[serde(default)]
    pub checked_refs: Option<Vec<String>>,
    pub assurance: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClaimWriteResult {
    pub claim: Claim,
    #[serde(default)]
    pub edges: Vec<ClaimEdge>,
    #[serde(default)]
    pub anchors: Vec<Anchor>,
    #[serde(default)]
    pub findings: Vec<ClaimFinding>,
}

fn check_fraction(value: f64) -> Result<()> {
    if !(0.0..=1.0).contains(&value) {
        return Err(Error::Validation(
            "confidence must be between 0 and 1".to_owned(),
        ));
    }
    Ok(())
}
