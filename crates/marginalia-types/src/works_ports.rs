//! Works-authorship port traits: the narrow seams the Phase 4 pure core
//! needs that the Phase 0 repository traits do not cover.
//!
//! Python sources: `services/works/{verify,validate,trace,attach,drafting,
//! publication}.py` (the repo/verification collaborators) and
//! `services/text/dates.py` (no collaborators — pure).
//!
//! Every trait here is unimplemented in Rust: fakes live in tests, real
//! adapters land with Phase 5. Services take them as generics so the rules
//! compile and run today against in-memory doubles.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::errors::Result;

/// Quote-verification tier, mirroring `services/verification/quote.py::Tier`
/// (and `marginalia_text::quote::Tier`) without pulling that crate in —
/// dependency direction stays `works -> {types, text}`, never
/// `types -> text`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VerifyTier {
    Exact,
    Normalized,
    Near,
    NotFound,
    NoCanonicalText,
}

impl VerifyTier {
    /// The `tier.value` string Python interpolates into findings and footnotes.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Exact => "exact",
            Self::Normalized => "normalized",
            Self::Near => "near",
            Self::NotFound => "not_found",
            Self::NoCanonicalText => "no_canonical_text",
        }
    }
}

/// Where a verified quotation sits: the fields of `QuoteLocation` the works
/// rules actually read.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VerifyLocation {
    pub document_id: Uuid,
    pub char_start: i64,
    pub char_end: i64,
}

/// The fields of `Divergence` the works rules copy into finding detail.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VerifyDivergence {
    pub matched_characters: i64,
    pub matched_tail: String,
    pub quote_continues: String,
    pub source_continues: String,
}

/// The fields of `QuoteVerification` the works rules actually read.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VerifyResult {
    pub tier: VerifyTier,
    #[serde(default)]
    pub location: Option<VerifyLocation>,
    #[serde(default)]
    pub matched_fraction: Option<f64>,
    #[serde(default)]
    pub divergence: Option<VerifyDivergence>,
}

/// The quote verifier behind `WorkVerifier`, `WorkRenderer`, and
/// `CitationService`: `QuoteVerifier.verify(quote, document_id, window)`.
pub trait VerifyPort: Send + Sync {
    async fn verify(
        &self,
        quote: &str,
        document_id: Option<Uuid>,
        window: Option<(i64, i64)>,
    ) -> Result<VerifyResult>;
}

/// One transaction factory for the write services (`attach`, `import_draft`,
/// `freeze`, `publish`): Python's `transaction_factory()` async context
/// manager, spelled as explicit begin/commit/rollback so dry-run and
/// refusal-path rollbacks stay visible at the call site.
pub trait TxFactory: Send + Sync {
    type Tx: Send;
    async fn begin(&self) -> Result<Self::Tx>;
    async fn commit(&self, tx: Self::Tx) -> Result<()>;
    async fn rollback(&self, tx: Self::Tx) -> Result<()>;
}

/// The drift check's exporter callback: renders the work's current draft to
/// markdown (`WorkExportService.export_draft_text`), kept behind a trait so
/// `WorkValidationService` stays constructible where the exporter is not.
pub trait DraftExporter: Send + Sync {
    async fn export_markdown(&self, work_id: Uuid) -> Result<String>;
}
