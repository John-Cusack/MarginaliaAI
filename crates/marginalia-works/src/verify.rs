//! Verify a work file's citations against the corpus.
//!
//! Python source: `services/works/verify.py`. A work is review-ready when
//! every entry verifies `exact` or `normalized` against the span it names.
//! Checks run per entry in a fixed order — validity, document, text, quote,
//! span, region, edition, key, marker — stopping at the first hard failure
//! per entry, then the work-level checks. Rule ids are the contract;
//! messages are for humans.

use std::collections::HashSet;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use marginalia_types::ports::{ClaimRepo, DocumentRepo, DocumentTextRepo, PassageRepo};
use marginalia_types::works_files::{CitationEntry, Intent, WorkFile, WorkFileStatus};
use marginalia_types::works_ports::{VerifyPort, VerifyTier};
use marginalia_types::Result;

use crate::files::WorkFileReader;

/// A quotation or translation citing this many characters or more is a
/// region by size, not by intent.
pub const MAX_QUOTE_CHARS: i64 = 1000;

/// Intents that must cite a narrowed span, never a whole passage.
pub fn is_narrow_intent(intent: &str) -> bool {
    matches!(intent, "quotation" | "translation")
}

/// Intents warned when they cite a whole passage.
pub fn is_region_warn_intent(intent: &str) -> bool {
    matches!(intent, "support" | "source" | "definition")
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Finding {
    pub rule_id: String,
    pub severity: String,
    #[serde(default)]
    pub citation_id: Option<String>,
    pub message: String,
    #[serde(default)]
    pub detail: Option<Map<String, Value>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CitationReport {
    pub id: String,
    pub intent: String,
    #[serde(default)]
    pub tier: Option<String>,
    pub document_id: String,
    pub char_start: i64,
    pub char_end: i64,
    #[serde(default)]
    pub edition_key: Option<String>,
    #[serde(default)]
    pub findings: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GateResult {
    pub name: String,
    pub passed: bool,
    #[serde(default)]
    pub blockers: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkReport {
    pub work_path: String,
    pub work: String,
    pub status: String,
    #[serde(default)]
    pub citations: Vec<CitationReport>,
    #[serde(default)]
    pub findings: Vec<Finding>,
    pub gate: GateResult,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct UnreadableEntry {
    pub work_path: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VerifySummary {
    pub works: i64,
    pub citations: i64,
    pub errors: i64,
    pub warnings: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VerifyOutput {
    #[serde(default)]
    pub works: Vec<WorkReport>,
    #[serde(default)]
    pub unreadable: Vec<UnreadableEntry>,
    pub summary: VerifySummary,
}

/// Rule ids that fail the gate: every error, plus edition identity at
/// `publish`. Sorted and deduplicated; empty at `none`.
pub fn gate_blockers(findings: &[Finding], gate: &str) -> Vec<String> {
    if gate == "none" {
        return Vec::new();
    }
    let mut blockers: HashSet<String> = HashSet::new();
    for finding in findings {
        if finding.severity == "error"
            || (gate == "publish" && finding.rule_id == "AUTH_CITATION_EDITION_MISSING")
        {
            blockers.insert(finding.rule_id.clone());
        }
    }
    let mut sorted: Vec<String> = blockers.into_iter().collect();
    sorted.sort();
    sorted
}

/// The gate passes when nothing blocks it.
pub fn gate_passes(findings: &[Finding], gate: &str) -> bool {
    gate_blockers(findings, gate).is_empty()
}

/// `Intent.value`: the citation's own vocabulary, as the file spells it.
pub(crate) fn intent_value(intent: &Intent) -> &'static str {
    match intent {
        Intent::Quotation => "quotation",
        Intent::Translation => "translation",
        Intent::Support => "support",
        Intent::Contrast => "contrast",
        Intent::Background => "background",
        Intent::Definition => "definition",
        Intent::Source => "source",
        Intent::SeeAlso => "see_also",
    }
}

fn file_status_value(status: &WorkFileStatus) -> &'static str {
    match status {
        WorkFileStatus::Draft => "draft",
        WorkFileStatus::Review => "review",
        WorkFileStatus::Published => "published",
    }
}

/// One entry's hard failure: `severity="error"` with the entry's id.
fn entry_error(entry: &CitationEntry, rule_id: &str, message: String) -> Finding {
    Finding {
        rule_id: rule_id.to_owned(),
        severity: "error".to_owned(),
        citation_id: Some(entry.id.clone()),
        message,
        detail: None,
    }
}

fn entry_error_detailed(
    entry: &CitationEntry,
    rule_id: &str,
    message: String,
    detail: Map<String, Value>,
) -> Finding {
    Finding {
        rule_id: rule_id.to_owned(),
        severity: "error".to_owned(),
        citation_id: Some(entry.id.clone()),
        message,
        detail: Some(detail),
    }
}

/// Render a metadata scalar the way an f-string does: strings raw, booleans
/// and nulls Python-spelled. Only strings occur in practice; the rest keeps
/// pathological metadata byte-identical instead of panicking.
fn metadata_scalar_text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Null => "None".to_owned(),
        other => other.to_string(),
    }
}

/// Per-entry checks in guide order, stopping at the first hard failure.
pub struct WorkVerifier<D, T, P, V, C> {
    documents: D,
    texts: T,
    passages: P,
    verification: V,
    claims: C,
    reader: WorkFileReader,
}

impl<D, T, P, V, C> WorkVerifier<D, T, P, V, C>
where
    D: DocumentRepo,
    T: DocumentTextRepo,
    P: PassageRepo,
    V: VerifyPort,
    C: ClaimRepo,
{
    pub fn new(
        documents: D,
        texts: T,
        passages: P,
        verification: V,
        claims: C,
        works_dir: PathBuf,
    ) -> Self {
        Self {
            documents,
            texts,
            passages,
            verification,
            claims,
            reader: WorkFileReader::new(works_dir),
        }
    }

    pub async fn verify_all(&self, gate: &str) -> Result<VerifyOutput> {
        let mut works = Vec::new();
        let mut unreadable = Vec::new();
        for work_path in self.reader.list_works() {
            match self.reader.read(&work_path) {
                // One bad file must not hide the rest.
                Err(error) => unreadable.push(UnreadableEntry {
                    work_path,
                    message: error.to_string(),
                }),
                Ok(work) => works.push(self.verify_parsed(&work, gate).await?),
            }
        }
        let summary = VerifySummary {
            works: works.len() as i64,
            citations: works.iter().map(|work| work.citations.len() as i64).sum(),
            errors: works
                .iter()
                .flat_map(|work| &work.findings)
                .filter(|finding| finding.severity == "error")
                .count() as i64,
            warnings: works
                .iter()
                .flat_map(|work| &work.findings)
                .filter(|finding| finding.severity == "warning")
                .count() as i64,
        };
        Ok(VerifyOutput {
            works,
            unreadable,
            summary,
        })
    }

    pub async fn verify_work(&self, work_path: &str, gate: &str) -> Result<WorkReport> {
        let work = self.reader.read(work_path)?;
        self.verify_parsed(&work, gate).await
    }

    /// The `verify_work(..., _work=...)` seam: validate an already-read file.
    pub async fn verify_parsed(&self, work: &WorkFile, gate: &str) -> Result<WorkReport> {
        let mut findings: Vec<Finding> = Vec::new();
        let mut citations: Vec<CitationReport> = Vec::new();

        for entry_error in &work.entry_errors {
            findings.push(Finding {
                rule_id: "AUTH_ENTRY_INVALID".to_owned(),
                severity: "error".to_owned(),
                citation_id: entry_error.citation_id.clone(),
                message: format!("Citation entry is invalid: {}", entry_error.message),
                detail: None,
            });
        }

        let markers: HashSet<String> = work.markers.iter().cloned().collect();
        let entry_ids: HashSet<&str> = work
            .front_matter
            .citations
            .iter()
            .map(|entry| entry.id.as_str())
            .collect();
        for entry in &work.front_matter.citations {
            let (report, entry_findings) = self.check_entry(entry, &markers, gate).await?;
            citations.push(report);
            findings.extend(entry_findings);
        }

        let error_ids: HashSet<&str> = work
            .entry_errors
            .iter()
            .filter_map(|error| error.citation_id.as_deref())
            .collect();
        for marker in &work.markers {
            if !entry_ids.contains(marker.as_str()) && !error_ids.contains(marker.as_str()) {
                findings.push(Finding {
                    rule_id: "AUTH_CITATION_MARKER_DANGLING".to_owned(),
                    severity: "error".to_owned(),
                    citation_id: Some(marker.clone()),
                    message: format!("Marker [^{marker}] has no citation entry"),
                    detail: None,
                });
            }
        }

        let claim_refs = &work.front_matter.claims;
        let existing_refs = self.claims.existing_refs(claim_refs).await?;
        for reference in claim_refs {
            if !existing_refs.contains(reference) {
                findings.push(Finding {
                    rule_id: "AUTH_CLAIM_UNRESOLVED".to_owned(),
                    severity: "error".to_owned(),
                    citation_id: None,
                    message: format!("Claim ref {reference} has no argument.claims row."),
                    detail: None,
                });
            }
        }

        if work.front_matter.status == WorkFileStatus::Published
            && !gate_passes(&findings, "publish")
        {
            findings.push(Finding {
                rule_id: "AUTH_STATUS_UNEARNED".to_owned(),
                severity: "error".to_owned(),
                citation_id: None,
                message: "The file says `published` but fails the publish gate".to_owned(),
                detail: None,
            });
        }
        let gate_passed = gate_passes(&findings, gate);
        let gate_blockers = gate_blockers(&findings, gate);
        Ok(WorkReport {
            work_path: work.work_path.clone(),
            work: work.front_matter.work.clone(),
            status: file_status_value(&work.front_matter.status).to_owned(),
            citations,
            findings,
            gate: GateResult {
                name: gate.to_owned(),
                passed: gate_passed,
                blockers: gate_blockers,
            },
        })
    }

    async fn check_entry(
        &self,
        entry: &CitationEntry,
        markers: &HashSet<String>,
        gate: &str,
    ) -> Result<(CitationReport, Vec<Finding>)> {
        let mut report = CitationReport {
            id: entry.id.clone(),
            intent: intent_value(&entry.intent).to_owned(),
            tier: None,
            document_id: entry.document_id.to_string(),
            char_start: entry.char_start,
            char_end: entry.char_end,
            edition_key: entry.edition_key.clone(),
            findings: Vec::new(),
        };
        let mut findings: Vec<Finding> = Vec::new();

        let document = self.documents.get(entry.document_id).await?;
        let Some(document) = document else {
            let finding = entry_error(
                entry,
                "AUTH_DOCUMENT_UNKNOWN",
                format!("Document {} does not exist", entry.document_id),
            );
            findings.push(finding);
            report.findings = findings
                .iter()
                .map(|finding| finding.rule_id.clone())
                .collect();
            return Ok((report, findings));
        };
        if self.texts.lengths(entry.document_id).await?.is_none() {
            let finding = entry_error(
                entry,
                "AUTH_SOURCE_UNCHECKABLE",
                format!(
                    "Document {} has no canonical text, so the quote cannot be checked",
                    entry.document_id
                ),
            );
            findings.push(finding);
            report.findings = findings
                .iter()
                .map(|finding| finding.rule_id.clone())
                .collect();
            return Ok((report, findings));
        }

        let result = self
            .verification
            .verify(
                &entry.quoted_text,
                Some(entry.document_id),
                Some((entry.char_start, entry.char_end)),
            )
            .await?;
        if matches!(
            result.tier,
            VerifyTier::Near | VerifyTier::NotFound | VerifyTier::NoCanonicalText
        ) {
            let mut detail = Map::new();
            detail.insert(
                "tier".to_owned(),
                Value::String(result.tier.as_str().to_owned()),
            );
            if let Some(matched_fraction) = result.matched_fraction {
                if let Some(number) = serde_json::Number::from_f64(matched_fraction) {
                    detail.insert("matched_fraction".to_owned(), Value::Number(number));
                }
            }
            if let Some(divergence) = &result.divergence {
                let mut dumped = Map::new();
                dumped.insert(
                    "matched_characters".to_owned(),
                    Value::Number(divergence.matched_characters.into()),
                );
                dumped.insert(
                    "matched_tail".to_owned(),
                    Value::String(divergence.matched_tail.clone()),
                );
                dumped.insert(
                    "quote_continues".to_owned(),
                    Value::String(divergence.quote_continues.clone()),
                );
                dumped.insert(
                    "source_continues".to_owned(),
                    Value::String(divergence.source_continues.clone()),
                );
                detail.insert("divergence".to_owned(), Value::Object(dumped));
            }
            let finding = entry_error_detailed(
                entry,
                "AUTH_QUOTE_UNVERIFIED",
                format!(
                    "Quote verifies {}, not exact or normalized",
                    result.tier.as_str()
                ),
                detail,
            );
            findings.push(finding);
            report.findings = findings
                .iter()
                .map(|finding| finding.rule_id.clone())
                .collect();
            return Ok((report, findings));
        }
        report.tier = Some(result.tier.as_str().to_owned());
        let location = result.location;
        let span_moved = match &location {
            None => true,
            Some(spot) => (spot.char_start, spot.char_end) != (entry.char_start, entry.char_end),
        };
        if span_moved {
            let mut detail = Map::new();
            detail.insert(
                "entry_span".to_owned(),
                Value::Array(vec![
                    Value::Number(entry.char_start.into()),
                    Value::Number(entry.char_end.into()),
                ]),
            );
            detail.insert(
                "verified_span".to_owned(),
                match &location {
                    Some(spot) => Value::Array(vec![
                        Value::Number(spot.char_start.into()),
                        Value::Number(spot.char_end.into()),
                    ]),
                    None => Value::Null,
                },
            );
            let finding = entry_error_detailed(
                entry,
                "AUTH_SOURCE_SPAN_STALE",
                "The verified span moved under this entry — re-anchor it".to_owned(),
                detail,
            );
            findings.push(finding);
            report.findings = findings
                .iter()
                .map(|finding| finding.rule_id.clone())
                .collect();
            return Ok((report, findings));
        }

        let region = self
            .is_region(entry.document_id, entry.char_start, entry.char_end)
            .await?;
        if is_narrow_intent(intent_value(&entry.intent))
            && (region || entry.char_end - entry.char_start > MAX_QUOTE_CHARS)
        {
            let finding = entry_error(
                entry,
                "AUTH_SPAN_NOT_NARROWED",
                "A quotation or translation must cite a narrowed span, not a whole passage"
                    .to_owned(),
            );
            findings.push(finding);
            report.findings = findings
                .iter()
                .map(|finding| finding.rule_id.clone())
                .collect();
            return Ok((report, findings));
        }
        if is_region_warn_intent(intent_value(&entry.intent)) && region {
            findings.push(Finding {
                rule_id: "AUTH_SPAN_REGION".to_owned(),
                severity: "warning".to_owned(),
                citation_id: Some(entry.id.clone()),
                message: "This span is exactly one passage — narrow it if the point rests on less"
                    .to_owned(),
                detail: None,
            });
        }

        if entry.edition_key.is_none() && entry.edition.is_none() {
            findings.push(Finding {
                rule_id: "AUTH_CITATION_EDITION_MISSING".to_owned(),
                severity: if gate == "publish" {
                    "error"
                } else {
                    "warning"
                }
                .to_owned(),
                citation_id: Some(entry.id.clone()),
                message: "Neither edition_key nor edition is set; the citation has no bibliographic identity"
                    .to_owned(),
                detail: None,
            });
        }
        if let Some(entry_key) = entry.edition_key.as_ref() {
            match document.metadata.get("edition_key") {
                None => findings.push(Finding {
                    rule_id: "AUTH_EDITION_KEY_UNKNOWN".to_owned(),
                    severity: "warning".to_owned(),
                    citation_id: Some(entry.id.clone()),
                    message: format!("edition_key {entry_key} is on no ingested document yet"),
                    detail: None,
                }),
                Some(Value::String(document_key)) if document_key == entry_key => {}
                Some(document_key) => {
                    let mut detail = Map::new();
                    detail.insert("entry_key".to_owned(), Value::String(entry_key.clone()));
                    detail.insert("document_key".to_owned(), document_key.clone());
                    findings.push(Finding {
                        rule_id: "AUTH_EDITION_KEY_MISMATCH".to_owned(),
                        severity: "error".to_owned(),
                        citation_id: Some(entry.id.clone()),
                        message: format!(
                            "Entry key {entry_key} differs from the document's {}",
                            metadata_scalar_text(document_key)
                        ),
                        detail: Some(detail),
                    });
                    report.findings = findings
                        .iter()
                        .map(|finding| finding.rule_id.clone())
                        .collect();
                    return Ok((report, findings));
                }
            }
        }

        if !markers.contains(&entry.id) {
            findings.push(Finding {
                rule_id: "AUTH_CITATION_MARKER_MISSING".to_owned(),
                severity: "warning".to_owned(),
                citation_id: Some(entry.id.clone()),
                message: format!(
                    "Entry {} never appears as [^{}] in the body",
                    entry.id, entry.id
                ),
                detail: None,
            });
        }

        report.findings = findings
            .iter()
            .map(|finding| finding.rule_id.clone())
            .collect();
        Ok((report, findings))
    }

    async fn is_region(&self, document_id: Uuid, char_start: i64, char_end: i64) -> Result<bool> {
        let covering = self
            .passages
            .covering_span(document_id, char_start, char_end)
            .await?;
        Ok(covering.iter().any(|passage| {
            passage.char_start == Some(char_start) && passage.char_end == Some(char_end)
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::future::Future;
    use std::sync::Mutex;

    use chrono::NaiveDate;
    use marginalia_types::documents::{Document, DocumentFilter, DocumentText};
    use marginalia_types::passages::{Passage, PassageDraft};
    use marginalia_types::ports::FilterExtension;
    use marginalia_types::works_files::{EntryError, Intent, WorkFrontMatter, WorkType};
    use marginalia_types::works_ports::{VerifyDivergence, VerifyLocation, VerifyResult};
    use marginalia_types::{Error, Result};
    use serde_json::{Map, Value};

    /// One recorded `VerifyPort::verify` call: quoted text, document, window.
    type VerifyCall = (String, Option<Uuid>, Option<(i64, i64)>);

    fn block_on<F: Future>(future: F) -> F::Output {
        // The fakes below never pend: every awaited sub-future is ready on
        // first poll, so a no-op waker with cooperative yielding terminates.
        use std::task::{Context, Poll, RawWaker, RawWakerVTable, Waker};
        fn raw() -> RawWaker {
            unsafe fn clone(_: *const ()) -> RawWaker {
                raw()
            }
            unsafe fn noop(_: *const ()) {}
            static TABLE: RawWakerVTable = RawWakerVTable::new(clone, noop, noop, noop);
            RawWaker::new(std::ptr::null(), &TABLE)
        }
        let waker = unsafe { Waker::from_raw(raw()) };
        let mut context = Context::from_waker(&waker);
        let mut pinned = Box::pin(future);
        loop {
            match pinned.as_mut().poll(&mut context) {
                Poll::Ready(value) => return value,
                Poll::Pending => std::thread::yield_now(),
            }
        }
    }

    fn doc_id() -> Uuid {
        // Hyphenated fixture id, mirroring the Python `DOC = uuid.uuid4()`.
        Uuid::from_u128(0x0123_4567_89ab_cdef_0123_4567_89ab_cdef)
    }

    fn test_date() -> NaiveDate {
        // Infallible calendar date: September has 30 days.
        NaiveDate::from_ymd_opt(2026, 9, 4).expect("2026-09-04 is a valid date")
    }

    struct FakeTexts {
        has_text: bool,
        texts: Mutex<HashMap<Uuid, DocumentText>>,
        fail_lengths: bool,
    }

    impl DocumentTextRepo for FakeTexts {
        type Tx = ();
        async fn put(
            &self,
            _tx: &mut Self::Tx,
            document_id: Uuid,
            text: &str,
            parser: &str,
            parser_version: &str,
        ) -> Result<()> {
            self.texts.lock().expect("fake lock").insert(
                document_id,
                DocumentText {
                    document_id,
                    text: text.to_owned(),
                    normalized_text: text.to_owned(),
                    normalization_version: "test".to_owned(),
                    parser: parser.to_owned(),
                    parser_version: parser_version.to_owned(),
                },
            );
            Ok(())
        }
        async fn get(&self, document_id: Uuid) -> Result<Option<DocumentText>> {
            Ok(self
                .texts
                .lock()
                .expect("fake lock")
                .get(&document_id)
                .cloned())
        }
        async fn get_text(&self, document_id: Uuid) -> Result<Option<String>> {
            Ok(self
                .texts
                .lock()
                .expect("fake lock")
                .get(&document_id)
                .map(|row| row.text.clone()))
        }
        async fn parser_versions(&self, document_ids: &[Uuid]) -> Result<HashMap<Uuid, String>> {
            let texts = self.texts.lock().expect("fake lock");
            Ok(document_ids
                .iter()
                .filter_map(|id| texts.get(id).map(|row| (*id, row.parser_version.clone())))
                .collect())
        }
        async fn lengths(&self, document_id: Uuid) -> Result<Option<(i64, i64)>> {
            if self.fail_lengths {
                return Err(Error::Storage("texts broke".to_owned()));
            }
            let texts = self.texts.lock().expect("fake lock");
            Ok(texts
                .get(&document_id)
                .map(|row| (row.text.len() as i64, row.normalized_text.len() as i64))
                .or_else(|| self.has_text.then_some((500, 450))))
        }
        async fn missing_document_ids(&self, _limit: Option<i64>) -> Result<Vec<Uuid>> {
            // The fake knows no wider universe: nothing is ever missing.
            Ok(Vec::new())
        }
    }

    struct FakeDocuments {
        docs: Mutex<HashMap<Uuid, Document>>,
        fail: bool,
    }

    fn fake_document(id: Uuid, metadata: Map<String, Value>) -> Document {
        Document {
            id,
            title: Some("Dabaris".to_owned()),
            document_type: "generic".to_owned(),
            language: None,
            source: "test".to_owned(),
            content_hash: vec![0u8; 32],
            parser: "test".to_owned(),
            parser_version: "1".to_owned(),
            ingested_at: chrono::Utc::now(),
            created_date_start: None,
            created_date_end: None,
            created_precision: None,
            edition_id: None,
            metadata,
        }
    }
    impl DocumentRepo for FakeDocuments {
        type Tx = ();
        async fn insert(
            &self,
            _tx: &mut Self::Tx,
            draft: marginalia_types::documents::DocumentDraft,
        ) -> Result<Document> {
            let document = Document {
                id: Uuid::new_v4(),
                title: draft.title,
                document_type: draft.document_type,
                language: draft.language,
                source: draft.source,
                content_hash: draft.content_hash,
                parser: draft.parser,
                parser_version: draft.parser_version,
                ingested_at: chrono::Utc::now(),
                created_date_start: draft.created_date_start,
                created_date_end: draft.created_date_end,
                created_precision: draft.created_precision,
                edition_id: draft.edition_id,
                metadata: draft.metadata,
            };
            self.docs
                .lock()
                .expect("fake lock")
                .insert(document.id, document.clone());
            Ok(document)
        }
        async fn get(&self, doc_id: Uuid) -> Result<Option<Document>> {
            if self.fail {
                return Err(Error::Storage("docs broke".to_owned()));
            }
            Ok(self.docs.lock().expect("fake lock").get(&doc_id).cloned())
        }
        async fn get_many(&self, doc_ids: &[Uuid]) -> Result<Vec<Document>> {
            let docs = self.docs.lock().expect("fake lock");
            Ok(doc_ids
                .iter()
                .filter_map(|id| docs.get(id).cloned())
                .collect())
        }
        async fn find_by_hash(
            &self,
            content_hash: &[u8],
            source: &str,
        ) -> Result<Option<Document>> {
            Ok(self
                .docs
                .lock()
                .expect("fake lock")
                .values()
                .find(|doc| doc.content_hash == content_hash && doc.source == source)
                .cloned())
        }
        async fn find_by_edition_id(
            &self,
            _tx: &mut Self::Tx,
            edition_id: Uuid,
        ) -> Result<Option<Document>> {
            Ok(self
                .docs
                .lock()
                .expect("fake lock")
                .values()
                .find(|doc| doc.edition_id == Some(edition_id))
                .cloned())
        }
        async fn update_metadata(
            &self,
            doc_id: Uuid,
            patch: Map<String, Value>,
        ) -> Result<Document> {
            let mut docs = self.docs.lock().expect("fake lock");
            let Some(document) = docs.get_mut(&doc_id) else {
                return Err(Error::NotFound {
                    kind: "document",
                    id: doc_id.to_string(),
                });
            };
            document.metadata.extend(patch);
            Ok(document.clone())
        }
        async fn iter_by_filter(&self, _filter: &DocumentFilter) -> Result<Vec<Document>> {
            // The fake holds one corpus and no index: every stored row matches.
            Ok(self
                .docs
                .lock()
                .expect("fake lock")
                .values()
                .cloned()
                .collect())
        }
        async fn count(&self, _filter: Option<&DocumentFilter>) -> Result<i64> {
            Ok(self.docs.lock().expect("fake lock").len() as i64)
        }
        async fn delete(&self, doc_id: Uuid) -> Result<()> {
            self.docs.lock().expect("fake lock").remove(&doc_id);
            Ok(())
        }
    }

    struct FakePassages {
        regions: Vec<(i64, i64)>,
        stored: Mutex<HashMap<Uuid, Passage>>,
        embeddings: Mutex<HashMap<(Uuid, String, String), Vec<f64>>>,
        fail_covering: bool,
    }

    fn fake_passage(start: i64, end: i64) -> Passage {
        Passage {
            id: Uuid::from_u128(0xaaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa),
            document_id: doc_id(),
            position: 0,
            char_start: Some(start),
            char_end: Some(end),
            locator: Map::new(),
            text: "passage".to_owned(),
            token_count: None,
            chunker: "test".to_owned(),
            chunker_version: "1".to_owned(),
            metadata: Map::new(),
            node_id: None,
            content_hash: vec![0u8; 32],
            created_at: chrono::Utc::now(),
        }
    }

    impl PassageRepo for FakePassages {
        type Tx = ();
        async fn insert_many(
            &self,
            _tx: &mut Self::Tx,
            document_id: Uuid,
            drafts: Vec<PassageDraft>,
        ) -> Result<Vec<Passage>> {
            let mut stored = self.stored.lock().expect("fake lock");
            let mut rows = Vec::with_capacity(drafts.len());
            for draft in drafts {
                let passage = Passage {
                    id: Uuid::new_v4(),
                    document_id,
                    position: draft.position,
                    char_start: Some(draft.char_start),
                    char_end: Some(draft.char_end),
                    locator: draft.locator,
                    text: draft.text,
                    token_count: draft.token_count,
                    chunker: draft.chunker,
                    chunker_version: draft.chunker_version,
                    metadata: draft.metadata,
                    node_id: draft.node_id,
                    content_hash: vec![0u8; 32],
                    created_at: chrono::Utc::now(),
                };
                stored.insert(passage.id, passage.clone());
                rows.push(passage);
            }
            Ok(rows)
        }
        async fn get(&self, passage_id: Uuid) -> Result<Option<Passage>> {
            Ok(self
                .stored
                .lock()
                .expect("fake lock")
                .get(&passage_id)
                .cloned())
        }
        async fn get_by_document(&self, document_id: Uuid) -> Result<Vec<Passage>> {
            let mut rows: Vec<Passage> = self
                .stored
                .lock()
                .expect("fake lock")
                .values()
                .filter(|row| row.document_id == document_id)
                .cloned()
                .collect();
            rows.sort_by_key(|row| row.position);
            Ok(rows)
        }
        async fn get_context(
            &self,
            passage_id: Uuid,
            before: i64,
            after: i64,
        ) -> Result<(Vec<Passage>, Passage, Vec<Passage>)> {
            let stored = self.stored.lock().expect("fake lock");
            let center = stored
                .get(&passage_id)
                .cloned()
                .ok_or_else(|| Error::NotFound {
                    kind: "passage",
                    id: passage_id.to_string(),
                })?;
            let mut ordered: Vec<Passage> = stored
                .values()
                .filter(|row| row.document_id == center.document_id)
                .cloned()
                .collect();
            ordered.sort_by_key(|row| row.position);
            let at = ordered
                .iter()
                .position(|row| row.id == passage_id)
                .expect("center is stored");
            let from = at.saturating_sub(before.max(0) as usize);
            let to = (at + 1 + after.max(0) as usize).min(ordered.len());
            Ok((
                ordered[from..at].to_vec(),
                center,
                ordered[at + 1..to].to_vec(),
            ))
        }
        async fn vector_search(
            &self,
            query_embedding: &[f64],
            model: &str,
            model_version: &str,
            candidate_ids: Option<&[Uuid]>,
            k: i64,
        ) -> Result<Vec<(Uuid, f64)>> {
            let stored = self.stored.lock().expect("fake lock");
            let embeddings = self.embeddings.lock().expect("fake lock");
            let mut scored: Vec<(Uuid, f64)> = stored
                .values()
                .filter(|row| candidate_ids.is_none_or(|ids| ids.contains(&row.id)))
                .map(|row| {
                    let score = embeddings
                        .get(&(row.id, model.to_owned(), model_version.to_owned()))
                        .map(|vector| {
                            vector
                                .iter()
                                .zip(query_embedding.iter())
                                .map(|(a, b)| a * b)
                                .sum()
                        })
                        .unwrap_or(0.0);
                    (row.id, score)
                })
                .collect();
            scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
            scored.truncate(k.max(0) as usize);
            Ok(scored)
        }
        async fn keyword_search(
            &self,
            query: &str,
            _lang: Option<&str>,
            candidate_ids: Option<&[Uuid]>,
            k: i64,
        ) -> Result<Vec<(Uuid, f64)>> {
            let stored = self.stored.lock().expect("fake lock");
            let mut scored: Vec<(Uuid, f64)> = stored
                .values()
                .filter(|row| candidate_ids.is_none_or(|ids| ids.contains(&row.id)))
                .map(|row| (row.id, row.text.matches(query).count() as f64))
                .collect();
            scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
            scored.truncate(k.max(0) as usize);
            Ok(scored)
        }
        async fn store_embeddings(
            &self,
            _tx: &mut Self::Tx,
            passage_ids: &[Uuid],
            embeddings: &[Vec<f64>],
            model: &str,
            model_version: &str,
            _dim: i64,
        ) -> Result<()> {
            if passage_ids.len() != embeddings.len() {
                return Err(Error::Validation(format!(
                    "expected {} embeddings, got {}",
                    passage_ids.len(),
                    embeddings.len()
                )));
            }
            let stored = self.stored.lock().expect("fake lock");
            let mut table = self.embeddings.lock().expect("fake lock");
            for (id, vector) in passage_ids.iter().zip(embeddings.iter()) {
                if !stored.contains_key(id) {
                    return Err(Error::NotFound {
                        kind: "passage",
                        id: id.to_string(),
                    });
                }
                table.insert(
                    (*id, model.to_owned(), model_version.to_owned()),
                    vector.clone(),
                );
            }
            Ok(())
        }
        async fn index_fts(
            &self,
            _tx: &mut Self::Tx,
            passage_ids: &[Uuid],
            texts: &[String],
            _lang: &str,
        ) -> Result<()> {
            // The fake keeps no FTS index: only the shape is checked.
            if passage_ids.len() != texts.len() {
                return Err(Error::Validation(format!(
                    "expected {} texts, got {}",
                    passage_ids.len(),
                    texts.len()
                )));
            }
            Ok(())
        }
        async fn get_embedding(
            &self,
            passage_id: Uuid,
            model: &str,
            model_version: &str,
        ) -> Result<Option<Vec<f64>>> {
            Ok(self
                .embeddings
                .lock()
                .expect("fake lock")
                .get(&(passage_id, model.to_owned(), model_version.to_owned()))
                .cloned())
        }
        async fn filter_candidate_ids<F: FilterExtension>(
            &self,
            _filters: &Map<String, Value>,
            _filter_extensions: Option<&HashMap<String, F>>,
        ) -> Result<Vec<Uuid>> {
            // The fake has no index to filter with: every stored row matches.
            Ok(self
                .stored
                .lock()
                .expect("fake lock")
                .keys()
                .cloned()
                .collect())
        }
        async fn covering_span(
            &self,
            _document_id: Uuid,
            _char_start: i64,
            _char_end: i64,
        ) -> Result<Vec<Passage>> {
            if self.fail_covering {
                return Err(Error::Storage("passages broke".to_owned()));
            }
            Ok(self
                .regions
                .iter()
                .map(|(start, end)| fake_passage(*start, *end))
                .collect())
        }
        async fn count(&self) -> Result<i64> {
            Ok(self.stored.lock().expect("fake lock").len() as i64)
        }
    }

    struct FakeClaims {
        existing: Mutex<HashSet<String>>,
        calls: Mutex<Vec<Vec<String>>>,
        claims: Mutex<HashMap<String, marginalia_types::claims::Claim>>,
        fail_refs: bool,
        anchors: Mutex<HashMap<Uuid, marginalia_types::claims::Anchor>>,
        edges: Mutex<Vec<marginalia_types::claims::ClaimEdge>>,
    }

    impl ClaimRepo for FakeClaims {
        type Tx = ();
        async fn upsert_claim(
            &self,
            _tx: &mut Self::Tx,
            draft: marginalia_types::claims::ClaimDraft,
        ) -> Result<marginalia_types::claims::Claim> {
            let mut claims = self.claims.lock().expect("fake lock");
            let now = chrono::Utc::now();
            let row = claims
                .entry(draft.r#ref.clone())
                .and_modify(|row| {
                    row.statement = draft.statement.clone();
                    row.kind = draft.kind;
                    row.status = draft.status;
                    row.confidence = draft.confidence;
                    row.steelman = draft.steelman.clone();
                    row.public_ready = draft.public_ready;
                    row.academic_candidate = draft.academic_candidate;
                    row.attributes = draft.attributes.clone();
                    row.updated_at = now;
                })
                .or_insert_with(|| marginalia_types::claims::Claim {
                    id: Uuid::new_v4(),
                    r#ref: draft.r#ref.clone(),
                    statement: draft.statement.clone(),
                    kind: draft.kind,
                    status: draft.status,
                    confidence: draft.confidence,
                    steelman: draft.steelman.clone(),
                    public_ready: draft.public_ready,
                    academic_candidate: draft.academic_candidate,
                    attributes: draft.attributes.clone(),
                    created_at: now,
                    updated_at: now,
                })
                .clone();
            self.existing
                .lock()
                .expect("fake lock")
                .insert(row.r#ref.clone());
            Ok(row)
        }
        async fn add_edge(
            &self,
            _tx: &mut Self::Tx,
            source_id: Uuid,
            target_id: Uuid,
            relation: marginalia_types::claims::ClaimRelation,
            confidence: Option<f64>,
            note: Option<&str>,
        ) -> Result<marginalia_types::claims::ClaimEdge> {
            let edge = marginalia_types::claims::ClaimEdge {
                id: Uuid::new_v4(),
                source_id,
                target_id,
                relation,
                confidence,
                note: note.map(str::to_owned),
                created_at: chrono::Utc::now(),
            };
            self.edges.lock().expect("fake lock").push(edge.clone());
            Ok(edge)
        }
        async fn add_anchor(
            &self,
            _tx: &mut Self::Tx,
            claim_id: Uuid,
            draft: marginalia_types::claims::AnchorDraft,
        ) -> Result<marginalia_types::claims::Anchor> {
            let anchor = marginalia_types::claims::Anchor {
                id: Uuid::new_v4(),
                claim_id,
                role: draft.role,
                person_entity_id: draft.person_entity_id,
                source_span_id: draft.source_span_id,
                quoted_text: draft.quoted_text.clone(),
                verify_status: Some(draft.verify_status),
                verified_at: Some(draft.verified_at),
                parser_version: draft.parser_version.clone(),
                edition_id: draft.edition_id,
                edition_key: draft.edition_key.clone(),
                locator: draft.locator.clone(),
                created_at: chrono::Utc::now(),
            };
            self.anchors
                .lock()
                .expect("fake lock")
                .insert(anchor.id, anchor.clone());
            Ok(anchor)
        }
        async fn existing_refs(&self, refs: &[String]) -> Result<HashSet<String>> {
            if self.fail_refs {
                return Err(Error::Storage("claims broke".to_owned()));
            }
            self.calls.lock().unwrap().push(refs.to_vec());
            let existing = self.existing.lock().expect("fake lock");
            Ok(refs
                .iter()
                .filter(|reference| existing.contains(*reference))
                .cloned()
                .collect())
        }
        async fn get_by_ref(&self, ref_: &str) -> Result<Option<marginalia_types::claims::Claim>> {
            Ok(self.claims.lock().expect("fake lock").get(ref_).cloned())
        }
        async fn anchors_for(
            &self,
            claim_id: Uuid,
        ) -> Result<Vec<marginalia_types::claims::Anchor>> {
            Ok(self
                .anchors
                .lock()
                .expect("fake lock")
                .values()
                .filter(|anchor| anchor.claim_id == claim_id)
                .cloned()
                .collect())
        }
        async fn anchor_by_id(
            &self,
            anchor_id: Uuid,
        ) -> Result<Option<marginalia_types::claims::Anchor>> {
            Ok(self
                .anchors
                .lock()
                .expect("fake lock")
                .get(&anchor_id)
                .cloned())
        }
        async fn edges_for(
            &self,
            claim_id: Uuid,
        ) -> Result<Vec<marginalia_types::claims::ClaimEdge>> {
            Ok(self
                .edges
                .lock()
                .expect("fake lock")
                .iter()
                .filter(|edge| edge.source_id == claim_id || edge.target_id == claim_id)
                .cloned()
                .collect())
        }
        async fn audit(
            &self,
            refs: Option<&[String]>,
        ) -> Result<marginalia_types::claims::ClaimAuditReport> {
            Ok(marginalia_types::claims::ClaimAuditReport {
                findings: Vec::new(),
                checked_refs: refs.map(|refs| refs.to_vec()),
                assurance: marginalia_types::claims::CLAIM_AUDIT_ASSURANCE.to_owned(),
            })
        }
    }

    struct FakeVerification {
        tier: VerifyTier,
        span: Option<(i64, i64)>,
        matched_fraction: Option<f64>,
        divergence: Option<VerifyDivergence>,
        calls: Mutex<Vec<VerifyCall>>,
        fail: bool,
    }

    impl VerifyPort for FakeVerification {
        async fn verify(
            &self,
            quote: &str,
            document_id: Option<Uuid>,
            window: Option<(i64, i64)>,
        ) -> Result<VerifyResult> {
            if self.fail {
                return Err(Error::Storage("verify broke".to_owned()));
            }
            self.calls
                .lock()
                .unwrap()
                .push((quote.to_owned(), document_id, window));
            Ok(VerifyResult {
                tier: self.tier,
                location: self.span.map(|(start, end)| VerifyLocation {
                    document_id: doc_id(),
                    char_start: start,
                    char_end: end,
                }),
                matched_fraction: self.matched_fraction,
                divergence: self.divergence.clone(),
            })
        }
    }

    struct Fakes {
        has_text: bool,
        docs: Option<HashMap<Uuid, Map<String, Value>>>,
        regions: Vec<(i64, i64)>,
        tier: VerifyTier,
        span: Option<(i64, i64)>,
        matched_fraction: Option<f64>,
        divergence: Option<VerifyDivergence>,
        claims: HashSet<String>,
        fail_docs: bool,
        fail_texts: bool,
        fail_passages: bool,
        fail_verify: bool,
        fail_claims: bool,
    }

    impl Default for Fakes {
        fn default() -> Self {
            let mut metadata = Map::new();
            metadata.insert(
                "edition_key".to_owned(),
                Value::String("DABAR_2026".to_owned()),
            );
            Self {
                has_text: true,
                docs: Some(HashMap::from([(doc_id(), metadata)])),
                regions: Vec::new(),
                tier: VerifyTier::Exact,
                span: Some((10, 60)),
                matched_fraction: None,
                divergence: None,
                claims: HashSet::new(),
                fail_docs: false,
                fail_texts: false,
                fail_passages: false,
                fail_verify: false,
                fail_claims: false,
            }
        }
    }

    fn make_entry(
        id: &str,
        intent: Intent,
        start: i64,
        end: i64,
        edition_key: Option<&str>,
    ) -> CitationEntry {
        CitationEntry {
            id: id.to_owned(),
            intent,
            role: None,
            document_id: doc_id(),
            char_start: start,
            char_end: end,
            quoted_text: "a fine sentence here".to_owned(),
            edition: None,
            edition_key: edition_key.map(str::to_owned),
            locator: Map::new(),
        }
    }

    fn default_entry() -> CitationEntry {
        make_entry("c1", Intent::Quotation, 10, 60, Some("DABAR_2026"))
    }

    fn make_work(
        entries: Vec<CitationEntry>,
        markers: Vec<&str>,
        claims: Vec<&str>,
        status: WorkFileStatus,
        entry_errors: Vec<EntryError>,
    ) -> WorkFile {
        WorkFile {
            work_path: "essay.md".to_owned(),
            front_matter: WorkFrontMatter {
                work: "W-001".to_owned(),
                title: "A fragment".to_owned(),
                work_type: WorkType::Essay,
                status,
                created: test_date(),
                claims: claims.into_iter().map(str::to_owned).collect(),
                citations: entries,
            },
            front_matter_sha: "0".repeat(64),
            body: String::new(),
            markers: markers.into_iter().map(str::to_owned).collect(),
            entry_errors,
        }
    }

    fn default_work(entries: Vec<CitationEntry>) -> WorkFile {
        make_work(entries, vec!["c1"], vec![], WorkFileStatus::Draft, vec![])
    }

    fn verifier_with(
        fakes: Fakes,
        dir: std::path::PathBuf,
    ) -> WorkVerifier<FakeDocuments, FakeTexts, FakePassages, FakeVerification, FakeClaims> {
        let verification = FakeVerification {
            tier: fakes.tier,
            span: fakes.span,
            matched_fraction: fakes.matched_fraction,
            divergence: fakes.divergence,
            calls: Mutex::new(Vec::new()),
            fail: fakes.fail_verify,
        };
        let claims = FakeClaims {
            existing: Mutex::new(fakes.claims),
            calls: Mutex::new(Vec::new()),
            claims: Mutex::new(HashMap::new()),
            anchors: Mutex::new(HashMap::new()),
            edges: Mutex::new(Vec::new()),
            fail_refs: fakes.fail_claims,
        };
        WorkVerifier::new(
            FakeDocuments {
                docs: Mutex::new(
                    fakes
                        .docs
                        .unwrap_or_default()
                        .into_iter()
                        .map(|(id, metadata)| (id, fake_document(id, metadata)))
                        .collect(),
                ),
                fail: fakes.fail_docs,
            },
            FakeTexts {
                has_text: fakes.has_text,
                texts: Mutex::new(HashMap::new()),
                fail_lengths: fakes.fail_texts,
            },
            FakePassages {
                regions: fakes.regions,
                stored: Mutex::new(HashMap::new()),
                embeddings: Mutex::new(HashMap::new()),
                fail_covering: fakes.fail_passages,
            },
            verification,
            claims,
            dir,
        )
    }

    fn verify_with(
        fakes: Fakes,
        work: &WorkFile,
        gate: &str,
    ) -> (WorkReport, Vec<VerifyCall>, Vec<Vec<String>>) {
        let verifier = verifier_with(fakes, std::env::temp_dir());
        // `WorkFileReader::new` only stores the directory; no I/O happens.
        let report = block_on(verifier.verify_parsed(work, gate))
            .expect("fake repos never fail the exercised paths");
        let calls = verifier.verification.calls.lock().unwrap().clone();
        let claim_calls = verifier.claims.calls.lock().unwrap().clone();
        (report, calls, claim_calls)
    }

    fn for_entry(report: &WorkReport, entry_id: &str) -> Vec<String> {
        report
            .findings
            .iter()
            .filter(|finding| finding.citation_id.as_deref() == Some(entry_id))
            .map(|finding| finding.rule_id.clone())
            .collect()
    }

    fn finding<'a>(report: &'a WorkReport, rule_id: &str) -> &'a Finding {
        report
            .findings
            .iter()
            .find(|finding| finding.rule_id == rule_id)
            .unwrap_or_else(|| panic!("expected finding {rule_id}"))
    }

    #[test]
    #[should_panic(expected = "expected finding NOPE")]
    fn test_finding_helper_names_the_missing_rule() {
        let report = WorkReport {
            work_path: "essay.md".to_owned(),
            work: "W-001".to_owned(),
            status: "draft".to_owned(),
            citations: Vec::new(),
            findings: Vec::new(),
            gate: GateResult {
                name: "none".to_owned(),
                passed: true,
                blockers: Vec::new(),
            },
        };
        finding(&report, "NOPE");
    }

    #[test]
    fn narrow_and_region_intent_sets() {
        assert!(is_narrow_intent("quotation"));
        assert!(is_narrow_intent("translation"));
        assert!(!is_narrow_intent("support"));
        assert!(!is_narrow_intent("background"));
        assert!(is_region_warn_intent("support"));
        assert!(is_region_warn_intent("source"));
        assert!(is_region_warn_intent("definition"));
        assert!(!is_region_warn_intent("quotation"));
        assert!(!is_region_warn_intent("background"));
    }

    #[test]
    fn gate_none_lists_but_never_blocks() {
        let findings = vec![
            Finding {
                rule_id: "AUTH_QUOTE_UNVERIFIED".to_owned(),
                severity: "error".to_owned(),
                citation_id: Some("c1".to_owned()),
                message: "bad".to_owned(),
                detail: None,
            },
            Finding {
                rule_id: "AUTH_CITATION_EDITION_MISSING".to_owned(),
                severity: "warning".to_owned(),
                citation_id: Some("c1".to_owned()),
                message: "missing".to_owned(),
                detail: None,
            },
        ];
        assert_eq!(gate_blockers(&findings, "none"), Vec::<String>::new());
        assert!(gate_passes(&findings, "none"));
    }

    #[test]
    fn gate_blockers_sort_and_deduplicate() {
        let findings = vec![
            Finding {
                rule_id: "AUTH_QUOTE_UNVERIFIED".to_owned(),
                severity: "error".to_owned(),
                citation_id: Some("c1".to_owned()),
                message: "bad".to_owned(),
                detail: None,
            },
            Finding {
                rule_id: "AUTH_DOCUMENT_UNKNOWN".to_owned(),
                severity: "error".to_owned(),
                citation_id: Some("c2".to_owned()),
                message: "gone".to_owned(),
                detail: None,
            },
            Finding {
                rule_id: "AUTH_DOCUMENT_UNKNOWN".to_owned(),
                severity: "error".to_owned(),
                citation_id: Some("c3".to_owned()),
                message: "gone".to_owned(),
                detail: None,
            },
        ];
        assert_eq!(
            gate_blockers(&findings, "review"),
            vec![
                "AUTH_DOCUMENT_UNKNOWN".to_owned(),
                "AUTH_QUOTE_UNVERIFIED".to_owned()
            ]
        );
        assert!(!gate_passes(&findings, "review"));
    }

    #[test]
    fn missing_edition_blocks_only_publish() {
        let findings = vec![Finding {
            rule_id: "AUTH_CITATION_EDITION_MISSING".to_owned(),
            severity: "warning".to_owned(),
            citation_id: Some("c1".to_owned()),
            message: "missing".to_owned(),
            detail: None,
        }];
        assert!(gate_passes(&findings, "review"));
        assert_eq!(gate_blockers(&findings, "review"), Vec::<String>::new());
        assert!(!gate_passes(&findings, "publish"));
        assert_eq!(
            gate_blockers(&findings, "publish"),
            vec!["AUTH_CITATION_EDITION_MISSING".to_owned()]
        );
    }

    #[test]
    fn known_claim_refs_have_no_finding_and_resolve_as_one_set() {
        let fakes = Fakes {
            claims: HashSet::from(["KNOWN-001".to_owned(), "KNOWN-002".to_owned()]),
            ..Default::default()
        };
        let work = make_work(
            vec![default_entry()],
            vec!["c1"],
            vec!["KNOWN-001", "KNOWN-002"],
            WorkFileStatus::Draft,
            vec![],
        );
        let (report, _, claim_calls) = verify_with(fakes, &work, "review");
        // The report is clean, so emptiness implies the rule's absence.
        assert!(report.findings.is_empty());
        assert_eq!(
            claim_calls,
            vec![vec!["KNOWN-001".to_owned(), "KNOWN-002".to_owned()]]
        );
    }

    #[test]
    fn unknown_claim_ref_is_an_error() {
        let work = make_work(
            vec![default_entry()],
            vec!["c1"],
            vec!["MISSING-001"],
            WorkFileStatus::Draft,
            vec![],
        );
        let (report, _, _) = verify_with(Fakes::default(), &work, "review");
        let unresolved = finding(&report, "AUTH_CLAIM_UNRESOLVED");
        assert_eq!(unresolved.severity, "error");
        assert_eq!(
            unresolved.message,
            "Claim ref MISSING-001 has no argument.claims row."
        );
        assert_eq!(report.gate.blockers, vec!["AUTH_CLAIM_UNRESOLVED"]);
    }

    #[test]
    fn invalid_entry_reports_entry_invalid() {
        let work = make_work(
            vec![],
            vec![],
            vec![],
            WorkFileStatus::Draft,
            vec![EntryError {
                citation_id: Some("c9".to_owned()),
                message: "boom".to_owned(),
            }],
        );
        let (report, _, _) = verify_with(Fakes::default(), &work, "none");
        assert_eq!(for_entry(&report, "c9"), vec!["AUTH_ENTRY_INVALID"]);
        assert_eq!(
            finding(&report, "AUTH_ENTRY_INVALID").message,
            "Citation entry is invalid: boom"
        );
    }

    #[test]
    fn unknown_document() {
        let fakes = Fakes {
            docs: Some(HashMap::new()),
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![default_entry()]), "none");
        assert_eq!(for_entry(&report, "c1"), vec!["AUTH_DOCUMENT_UNKNOWN"]);
        assert_eq!(
            finding(&report, "AUTH_DOCUMENT_UNKNOWN").message,
            format!("Document {} does not exist", doc_id())
        );
    }

    #[test]
    fn uncheckable_source() {
        let fakes = Fakes {
            has_text: false,
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![default_entry()]), "none");
        assert_eq!(for_entry(&report, "c1"), vec!["AUTH_SOURCE_UNCHECKABLE"]);
        assert_eq!(
            finding(&report, "AUTH_SOURCE_UNCHECKABLE").message,
            format!(
                "Document {} has no canonical text, so the quote cannot be checked",
                doc_id()
            )
        );
    }

    #[test]
    fn unverified_quote_carries_tier_detail() {
        let fakes = Fakes {
            tier: VerifyTier::Near,
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![default_entry()]), "none");
        assert_eq!(for_entry(&report, "c1"), vec!["AUTH_QUOTE_UNVERIFIED"]);
        let unresolved = finding(&report, "AUTH_QUOTE_UNVERIFIED");
        assert_eq!(
            unresolved.message,
            "Quote verifies near, not exact or normalized"
        );
        let detail = unresolved.detail.as_ref().expect("tier detail");
        assert_eq!(detail.get("tier"), Some(&Value::String("near".to_owned())));
    }

    #[test]
    fn unverified_quote_detail_carries_fraction_and_divergence() {
        let fakes = Fakes {
            tier: VerifyTier::NotFound,
            span: None,
            matched_fraction: Some(0.5),
            divergence: Some(VerifyDivergence {
                matched_characters: 7,
                matched_tail: "tail".to_owned(),
                quote_continues: "qnext".to_owned(),
                source_continues: "snext".to_owned(),
            }),
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![default_entry()]), "none");
        let unresolved = finding(&report, "AUTH_QUOTE_UNVERIFIED");
        let detail = unresolved.detail.as_ref().expect("tier detail");
        assert_eq!(
            detail.get("tier"),
            Some(&Value::String("not_found".to_owned()))
        );
        assert_eq!(detail.get("matched_fraction"), Some(&Value::from(0.5f64)));
        let divergence = detail
            .get("divergence")
            .and_then(Value::as_object)
            .expect("divergence dump");
        assert_eq!(
            divergence.get("matched_characters"),
            Some(&Value::from(7i64))
        );
        assert_eq!(
            divergence.get("matched_tail"),
            Some(&Value::String("tail".to_owned()))
        );
        assert_eq!(
            divergence.get("quote_continues"),
            Some(&Value::String("qnext".to_owned()))
        );
        assert_eq!(
            divergence.get("source_continues"),
            Some(&Value::String("snext".to_owned()))
        );
    }

    #[test]
    fn stale_span_reports_both_spans() {
        let fakes = Fakes {
            span: Some((10, 80)),
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![default_entry()]), "none");
        assert_eq!(for_entry(&report, "c1"), vec!["AUTH_SOURCE_SPAN_STALE"]);
        let stale = finding(&report, "AUTH_SOURCE_SPAN_STALE");
        assert_eq!(
            stale.message,
            "The verified span moved under this entry — re-anchor it"
        );
        let detail = stale.detail.as_ref().expect("span detail");
        assert_eq!(
            detail.get("entry_span"),
            Some(&Value::Array(vec![Value::from(10i64), Value::from(60i64)]))
        );
        assert_eq!(
            detail.get("verified_span"),
            Some(&Value::Array(vec![Value::from(10i64), Value::from(80i64)]))
        );
    }

    #[test]
    fn missing_verified_span_is_stale_with_null() {
        let fakes = Fakes {
            span: None,
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![default_entry()]), "none");
        assert_eq!(for_entry(&report, "c1"), vec!["AUTH_SOURCE_SPAN_STALE"]);
        let detail = finding(&report, "AUTH_SOURCE_SPAN_STALE")
            .detail
            .as_ref()
            .expect("span detail");
        assert_eq!(detail.get("verified_span"), Some(&Value::Null));
    }

    #[test]
    fn quotation_on_a_region_is_not_narrowed() {
        let fakes = Fakes {
            regions: vec![(10, 60)],
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![default_entry()]), "none");
        assert_eq!(for_entry(&report, "c1"), vec!["AUTH_SPAN_NOT_NARROWED"]);
        assert_eq!(
            finding(&report, "AUTH_SPAN_NOT_NARROWED").message,
            "A quotation or translation must cite a narrowed span, not a whole passage"
        );
    }

    #[test]
    fn long_quotation_is_not_narrowed_without_any_region() {
        let entry = make_entry(
            "c1",
            Intent::Quotation,
            0,
            MAX_QUOTE_CHARS + 1,
            Some("DABAR_2026"),
        );
        let fakes = Fakes {
            span: Some((0, MAX_QUOTE_CHARS + 1)),
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![entry]), "none");
        assert_eq!(for_entry(&report, "c1"), vec!["AUTH_SPAN_NOT_NARROWED"]);
    }

    #[test]
    fn quotation_at_exactly_cap_is_narrowed() {
        // `>` is strict: a thousand characters is a quotation, a thousand
        // and one is a region by size.
        let entry = make_entry(
            "c1",
            Intent::Quotation,
            0,
            MAX_QUOTE_CHARS,
            Some("DABAR_2026"),
        );
        let fakes = Fakes {
            span: Some((0, MAX_QUOTE_CHARS)),
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![entry]), "none");
        assert_eq!(for_entry(&report, "c1"), Vec::<String>::new());
    }

    #[test]
    fn support_on_a_region_is_a_warning() {
        let entry = make_entry("c1", Intent::Support, 10, 60, Some("DABAR_2026"));
        let fakes = Fakes {
            regions: vec![(10, 60)],
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![entry]), "none");
        assert_eq!(for_entry(&report, "c1"), vec!["AUTH_SPAN_REGION"]);
        assert_eq!(finding(&report, "AUTH_SPAN_REGION").severity, "warning");
        assert!(report.gate.passed);
    }

    #[test]
    fn background_on_a_region_is_fine() {
        let entry = make_entry("c1", Intent::Background, 10, 60, Some("DABAR_2026"));
        let fakes = Fakes {
            regions: vec![(10, 60)],
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![entry]), "none");
        assert_eq!(for_entry(&report, "c1"), Vec::<String>::new());
    }

    #[test]
    fn missing_edition_is_a_review_warning() {
        let entry = make_entry("c1", Intent::Quotation, 10, 60, None);
        let (report, _, _) = verify_with(Fakes::default(), &default_work(vec![entry]), "none");
        assert_eq!(
            for_entry(&report, "c1"),
            vec!["AUTH_CITATION_EDITION_MISSING"]
        );
        assert_eq!(
            finding(&report, "AUTH_CITATION_EDITION_MISSING").severity,
            "warning"
        );
        assert!(report.gate.passed);
    }

    #[test]
    fn document_without_a_key_is_unknown() {
        let fakes = Fakes {
            docs: Some(HashMap::from([(doc_id(), Map::new())])),
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![default_entry()]), "none");
        assert_eq!(for_entry(&report, "c1"), vec!["AUTH_EDITION_KEY_UNKNOWN"]);
        assert_eq!(
            finding(&report, "AUTH_EDITION_KEY_UNKNOWN").message,
            "edition_key DABAR_2026 is on no ingested document yet"
        );
    }

    #[test]
    fn differing_key_is_a_mismatch_with_early_return() {
        let mut metadata = Map::new();
        metadata.insert("edition_key".to_owned(), Value::String("OTHER".to_owned()));
        let fakes = Fakes {
            docs: Some(HashMap::from([(doc_id(), metadata)])),
            ..Default::default()
        };
        // No marker: the mismatch returns before the marker check runs.
        let work = make_work(
            vec![default_entry()],
            vec![],
            vec![],
            WorkFileStatus::Draft,
            vec![],
        );
        let (report, _, _) = verify_with(fakes, &work, "none");
        assert_eq!(for_entry(&report, "c1"), vec!["AUTH_EDITION_KEY_MISMATCH"]);
        let mismatch = finding(&report, "AUTH_EDITION_KEY_MISMATCH");
        assert_eq!(
            mismatch.message,
            "Entry key DABAR_2026 differs from the document's OTHER"
        );
        let detail = mismatch.detail.as_ref().expect("key detail");
        assert_eq!(
            detail.get("entry_key"),
            Some(&Value::String("DABAR_2026".to_owned()))
        );
        assert_eq!(
            detail.get("document_key"),
            Some(&Value::String("OTHER".to_owned()))
        );
        assert_eq!(
            report.citations[0].findings,
            vec!["AUTH_EDITION_KEY_MISMATCH"]
        );
    }

    #[test]
    fn missing_marker() {
        let work = make_work(
            vec![default_entry()],
            vec![],
            vec![],
            WorkFileStatus::Draft,
            vec![],
        );
        let (report, _, _) = verify_with(Fakes::default(), &work, "none");
        assert_eq!(
            for_entry(&report, "c1"),
            vec!["AUTH_CITATION_MARKER_MISSING"]
        );
        assert_eq!(
            finding(&report, "AUTH_CITATION_MARKER_MISSING").message,
            "Entry c1 never appears as [^c1] in the body"
        );
    }

    #[test]
    fn dangling_marker() {
        let work = make_work(
            vec![default_entry()],
            vec!["c1", "c7"],
            vec![],
            WorkFileStatus::Draft,
            vec![],
        );
        let (report, _, _) = verify_with(Fakes::default(), &work, "none");
        assert_eq!(
            for_entry(&report, "c7"),
            vec!["AUTH_CITATION_MARKER_DANGLING"]
        );
        assert_eq!(
            finding(&report, "AUTH_CITATION_MARKER_DANGLING").message,
            "Marker [^c7] has no citation entry"
        );
    }

    #[test]
    fn dangling_marker_skips_entry_error_ids() {
        let work = make_work(
            vec![],
            vec!["c9"],
            vec![],
            WorkFileStatus::Draft,
            vec![EntryError {
                citation_id: Some("c9".to_owned()),
                message: "boom".to_owned(),
            }],
        );
        let (report, _, _) = verify_with(Fakes::default(), &work, "none");
        assert!(!report
            .findings
            .iter()
            .any(|finding| finding.rule_id == "AUTH_CITATION_MARKER_DANGLING"));
    }

    #[test]
    fn clean_entry_reports_its_tier() {
        let (report, _, _) = verify_with(
            Fakes::default(),
            &default_work(vec![default_entry()]),
            "none",
        );
        assert_eq!(report.citations[0].tier, Some("exact".to_owned()));
        assert_eq!(report.citations[0].findings, Vec::<String>::new());
        assert_eq!(report.citations[0].document_id, doc_id().to_string());
        assert!(report.gate.passed);
    }

    #[test]
    fn normalized_tier_is_clean() {
        let fakes = Fakes {
            tier: VerifyTier::Normalized,
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![default_entry()]), "none");
        assert_eq!(report.citations[0].tier, Some("normalized".to_owned()));
        assert_eq!(for_entry(&report, "c1"), Vec::<String>::new());
    }

    #[test]
    fn verify_receives_the_entry_span_as_its_window() {
        let (report, calls, _) = verify_with(
            Fakes::default(),
            &default_work(vec![default_entry()]),
            "none",
        );
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "a fine sentence here");
        assert_eq!(calls[0].1, Some(doc_id()));
        assert_eq!(calls[0].2, Some((10, 60)));
        assert_eq!(report.citations.len(), 1);
    }

    #[test]
    fn review_fails_on_any_error() {
        let fakes = Fakes {
            tier: VerifyTier::Near,
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![default_entry()]), "review");
        assert!(!report.gate.passed);
        assert_eq!(report.gate.blockers, vec!["AUTH_QUOTE_UNVERIFIED"]);
    }

    #[test]
    fn publish_fails_on_a_missing_edition() {
        let entry = make_entry("c1", Intent::Quotation, 10, 60, None);
        let (report, _, _) = verify_with(Fakes::default(), &default_work(vec![entry]), "publish");
        assert!(!report.gate.passed);
        assert!(report
            .gate
            .blockers
            .contains(&"AUTH_CITATION_EDITION_MISSING".to_owned()));
        let missing = finding(&report, "AUTH_CITATION_EDITION_MISSING");
        assert_eq!(missing.severity, "error");
    }

    #[test]
    fn published_status_must_earn_it() {
        let entry = make_entry("c1", Intent::Quotation, 10, 60, None);
        let work = make_work(
            vec![entry],
            vec!["c1"],
            vec![],
            WorkFileStatus::Published,
            vec![],
        );
        let (report, _, _) = verify_with(Fakes::default(), &work, "review");
        let ids: Vec<&str> = report
            .findings
            .iter()
            .map(|finding| finding.rule_id.as_str())
            .collect();
        assert!(ids.contains(&"AUTH_STATUS_UNEARNED"));
        assert!(!report.gate.passed);
    }

    #[test]
    fn published_status_clean_passes() {
        let work = make_work(
            vec![default_entry()],
            vec!["c1"],
            vec![],
            WorkFileStatus::Published,
            vec![],
        );
        let (report, _, _) = verify_with(Fakes::default(), &work, "review");
        // The report is clean, so emptiness implies the rule's absence.
        assert!(report.findings.is_empty());
        assert!(report.gate.passed);
    }

    #[test]
    fn draft_with_warnings_passes_review() {
        let work = make_work(
            vec![default_entry()],
            vec![],
            vec![],
            WorkFileStatus::Draft,
            vec![],
        );
        let (report, _, _) = verify_with(Fakes::default(), &work, "review");
        assert!(report.gate.passed);
        assert_eq!(report.gate.blockers, Vec::<String>::new());
    }

    #[test]
    fn test_block_on_drives_a_pending_future_to_ready() {
        struct PendOnce(bool);
        impl Future for PendOnce {
            type Output = u32;
            fn poll(
                mut self: std::pin::Pin<&mut Self>,
                context: &mut std::task::Context<'_>,
            ) -> std::task::Poll<u32> {
                if self.0 {
                    std::task::Poll::Ready(7)
                } else {
                    self.0 = true;
                    context.waker().wake_by_ref();
                    std::task::Poll::Pending
                }
            }
        }
        assert_eq!(block_on(PendOnce(false)), 7);
    }

    #[test]
    fn test_block_on_survives_a_waker_clone() {
        struct CloneWaker;
        impl Future for CloneWaker {
            type Output = ();
            fn poll(
                self: std::pin::Pin<&mut Self>,
                context: &mut std::task::Context<'_>,
            ) -> std::task::Poll<()> {
                // The no-op waker supports clone: dropping the clone here
                // exercises the raw-waker clone path.
                let _ = context.waker().clone();
                std::task::Poll::Ready(())
            }
        }
        block_on(CloneWaker);
    }

    #[test]
    fn test_intent_and_status_values_cover_every_variant() {
        assert_eq!(intent_value(&Intent::Quotation), "quotation");
        assert_eq!(intent_value(&Intent::Translation), "translation");
        assert_eq!(intent_value(&Intent::Support), "support");
        assert_eq!(intent_value(&Intent::Contrast), "contrast");
        assert_eq!(intent_value(&Intent::Background), "background");
        assert_eq!(intent_value(&Intent::Definition), "definition");
        assert_eq!(intent_value(&Intent::Source), "source");
        assert_eq!(intent_value(&Intent::SeeAlso), "see_also");
        assert_eq!(file_status_value(&WorkFileStatus::Draft), "draft");
        assert_eq!(file_status_value(&WorkFileStatus::Review), "review");
        assert_eq!(file_status_value(&WorkFileStatus::Published), "published");
    }

    #[test]
    fn test_metadata_scalar_spells_every_json_shape() {
        assert_eq!(
            metadata_scalar_text(&Value::String("DABAR_2026".to_owned())),
            "DABAR_2026"
        );
        assert_eq!(metadata_scalar_text(&Value::Bool(true)), "True");
        assert_eq!(metadata_scalar_text(&Value::Bool(false)), "False");
        assert_eq!(metadata_scalar_text(&Value::Null), "None");
        assert_eq!(metadata_scalar_text(&Value::from(5)), "5");
    }

    /// One on-disk entry that verifies cleanly: known document, exact tier,
    /// matching span and edition, marker present.
    fn on_disk_entry_text() -> String {
        format!(
            "---\n\
             work: W-001\n\
             title: \"A fragment\"\n\
             type: essay\n\
             status: draft\n\
             created: 2026-09-04\n\
             claims: []\n\
             citations:\n\
             \x20 - id: c1\n\
             \x20   document_id: {}\n\
             \x20   char_start: 10\n\
             \x20   char_end: 60\n\
             \x20   quoted_text: \"a fine sentence here\"\n\
             \x20   intent: quotation\n\
             \x20   edition_key: DABAR_2026\n\
             ---\n\nBody with [^c1] marker.\n",
            doc_id()
        )
    }

    fn dir_verifier(
        dir: &std::path::Path,
    ) -> WorkVerifier<FakeDocuments, FakeTexts, FakePassages, FakeVerification, FakeClaims> {
        dir_verifier_with(dir, Fakes::default())
    }

    fn dir_verifier_with(
        dir: &std::path::Path,
        fakes: Fakes,
    ) -> WorkVerifier<FakeDocuments, FakeTexts, FakePassages, FakeVerification, FakeClaims> {
        verifier_with(fakes, dir.to_path_buf())
    }

    #[test]
    fn test_verify_all_reads_every_file_and_counts_the_summary() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("essay.md"), on_disk_entry_text()).expect("fixture write");
        std::fs::write(dir.path().join("broken.md"), "no fence at all\n").expect("fixture write");
        let verifier = dir_verifier(dir.path());

        let output = block_on(verifier.verify_all("none")).expect("verify_all reads");

        // One bad file hides neither the good work nor itself.
        assert_eq!(output.works.len(), 1);
        assert_eq!(output.works[0].work_path, "essay.md");
        assert!(output.works[0].gate.passed);
        assert_eq!(output.unreadable.len(), 1);
        assert_eq!(output.unreadable[0].work_path, "broken.md");
        assert!(!output.unreadable[0].message.is_empty());
        assert_eq!(output.summary.works, 1);
        assert_eq!(output.summary.citations, 1);
        assert_eq!(output.summary.errors, 0);
        assert_eq!(output.summary.warnings, 0);
    }

    #[test]
    fn test_verify_work_reads_one_file_by_path() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("essay.md"), on_disk_entry_text()).expect("fixture write");
        let verifier = dir_verifier(dir.path());

        let report = block_on(verifier.verify_work("essay.md", "none")).expect("verify_work reads");

        assert_eq!(report.work_path, "essay.md");
        assert_eq!(report.status, "draft");
        assert!(report.gate.passed);
        assert_eq!(report.citations.len(), 1);
        assert_eq!(report.citations[0].tier.as_deref(), Some("exact"));
        let err =
            block_on(verifier.verify_work("gone.md", "none")).expect_err("missing file fails");
        assert!(err.to_string().contains("Cannot read gone.md: "), "{err}");
    }
    #[test]
    fn test_fake_texts_round_trip_every_method() {
        let repo = FakeTexts {
            has_text: true,
            texts: Mutex::new(HashMap::new()),
            fail_lengths: false,
        };
        let id = doc_id();
        assert_eq!(
            block_on(repo.lengths(id)).expect("lengths"),
            Some((500, 450))
        );
        block_on(repo.put(&mut (), id, "canonical words", "test", "pv2")).expect("put stores");
        assert_eq!(block_on(repo.lengths(id)).expect("lengths"), Some((15, 15)));
        assert_eq!(
            block_on(repo.get(id))
                .expect("get")
                .expect("present")
                .parser_version,
            "pv2"
        );
        assert!(block_on(repo.get(Uuid::new_v4())).expect("get").is_none());
        assert_eq!(
            block_on(repo.get_text(id)).expect("text"),
            Some("canonical words".to_owned())
        );
        assert!(block_on(repo.get_text(Uuid::new_v4()))
            .expect("text")
            .is_none());
        assert_eq!(
            block_on(repo.parser_versions(&[id, Uuid::new_v4()]))
                .expect("versions")
                .get(&id),
            Some(&"pv2".to_owned())
        );
        assert!(block_on(repo.missing_document_ids(None))
            .expect("missing")
            .is_empty());

        let bare = FakeTexts {
            has_text: false,
            texts: Mutex::new(HashMap::new()),
            fail_lengths: false,
        };
        assert_eq!(block_on(bare.lengths(id)).expect("lengths"), None);
    }

    #[test]
    fn non_finite_fraction_is_dropped_from_the_detail() {
        // `Number::from_f64` is `None` for NaN: the tier still reports, but
        // no fraction serializes.
        let fakes = Fakes {
            tier: VerifyTier::NotFound,
            span: None,
            matched_fraction: Some(f64::NAN),
            ..Default::default()
        };
        let (report, _, _) = verify_with(fakes, &default_work(vec![default_entry()]), "none");
        assert_eq!(for_entry(&report, "c1"), vec!["AUTH_QUOTE_UNVERIFIED"]);
        let detail = finding(&report, "AUTH_QUOTE_UNVERIFIED")
            .detail
            .as_ref()
            .expect("tier detail");
        assert_eq!(
            detail.get("tier"),
            Some(&Value::String("not_found".to_owned()))
        );
        assert_eq!(detail.get("matched_fraction"), None);
    }

    #[test]
    fn document_failure_fails_the_entry() {
        let fakes = Fakes {
            fail_docs: true,
            ..Default::default()
        };
        let verifier = verifier_with(fakes, std::env::temp_dir());
        let error = block_on(verifier.verify_parsed(&default_work(vec![default_entry()]), "none"))
            .expect_err("document failure fails");
        assert_eq!(error.to_string(), "database or storage error: docs broke");
    }

    #[test]
    fn text_lengths_failure_fails_the_entry() {
        let fakes = Fakes {
            fail_texts: true,
            ..Default::default()
        };
        let verifier = verifier_with(fakes, std::env::temp_dir());
        let error = block_on(verifier.verify_parsed(&default_work(vec![default_entry()]), "none"))
            .expect_err("lengths failure fails");
        assert_eq!(error.to_string(), "database or storage error: texts broke");
    }

    #[test]
    fn verification_failure_fails_the_entry() {
        let fakes = Fakes {
            fail_verify: true,
            ..Default::default()
        };
        let verifier = verifier_with(fakes, std::env::temp_dir());
        let error = block_on(verifier.verify_parsed(&default_work(vec![default_entry()]), "none"))
            .expect_err("verification failure fails");
        assert_eq!(error.to_string(), "database or storage error: verify broke");
    }

    #[test]
    fn covering_span_failure_fails_the_entry() {
        let fakes = Fakes {
            fail_passages: true,
            ..Default::default()
        };
        let verifier = verifier_with(fakes, std::env::temp_dir());
        let error = block_on(verifier.verify_parsed(&default_work(vec![default_entry()]), "none"))
            .expect_err("covering failure fails");
        assert_eq!(
            error.to_string(),
            "database or storage error: passages broke"
        );
    }

    #[test]
    fn claim_refs_failure_fails_the_work() {
        let fakes = Fakes {
            fail_claims: true,
            ..Default::default()
        };
        let verifier = verifier_with(fakes, std::env::temp_dir());
        let error = block_on(verifier.verify_parsed(&default_work(vec![default_entry()]), "none"))
            .expect_err("claim refs failure fails");
        assert_eq!(error.to_string(), "database or storage error: claims broke");
    }

    #[test]
    fn test_verify_all_propagates_an_entry_failure() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("essay.md"), on_disk_entry_text()).expect("fixture write");
        let fakes = Fakes {
            fail_verify: true,
            ..Default::default()
        };
        let verifier = dir_verifier_with(dir.path(), fakes);
        let error = block_on(verifier.verify_all("none")).expect_err("entry failure fails the run");
        assert_eq!(error.to_string(), "database or storage error: verify broke");
    }

    #[test]
    fn test_verify_all_counts_findings_across_files() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("essay.md"), on_disk_entry_text()).expect("fixture write");
        // A marker without an entry is a dangling-marker error, so the
        // summary's severity filters all run over real findings.
        std::fs::write(
            dir.path().join("dangling.md"),
            "---\n\
             work: W-002\n\
             title: \"Dangling\"\n\
             type: essay\n\
             status: draft\n\
             created: 2026-09-04\n\
             claims: []\n\
             citations: []\n\
             ---\n\nPointing at [^c9] with no entry.\n",
        )
        .expect("fixture write");
        let verifier = dir_verifier(dir.path());

        let output = block_on(verifier.verify_all("none")).expect("verify_all reads");

        assert_eq!(output.summary.works, 2);
        assert_eq!(output.summary.citations, 1);
        assert_eq!(output.summary.errors, 1);
        assert_eq!(output.summary.warnings, 0);
    }

    #[test]
    fn test_verify_all_empty_dir_is_an_empty_report() {
        let dir = tempfile::tempdir().expect("tempdir");
        let verifier = dir_verifier(dir.path());

        let output = block_on(verifier.verify_all("none")).expect("verify_all reads");

        assert!(output.works.is_empty());
        assert!(output.unreadable.is_empty());
        assert_eq!(output.summary.works, 0);
        assert_eq!(output.summary.citations, 0);
        assert_eq!(output.summary.errors, 0);
        assert_eq!(output.summary.warnings, 0);
    }

    #[test]
    fn test_fake_documents_round_trip_every_method() {
        use marginalia_types::documents::{DocumentDraft, DocumentFilter};
        let repo = FakeDocuments {
            docs: Mutex::new(HashMap::new()),
            fail: false,
        };
        assert_eq!(block_on(repo.count(None)).expect("count"), 0);
        let draft = DocumentDraft {
            title: Some("Dabaris".to_owned()),
            document_type: "generic".to_owned(),
            language: None,
            source: "test".to_owned(),
            content_hash: vec![7u8; 32],
            parser: "test".to_owned(),
            parser_version: "1".to_owned(),
            created_date_start: None,
            created_date_end: None,
            created_precision: None,
            edition_id: None,
            metadata: Map::new(),
        };
        let inserted = block_on(repo.insert(&mut (), draft)).expect("insert stores");
        assert_eq!(block_on(repo.count(None)).expect("count"), 1);
        assert_eq!(
            block_on(repo.get(inserted.id))
                .expect("get")
                .expect("present")
                .title,
            Some("Dabaris".to_owned())
        );
        assert!(block_on(repo.get(Uuid::new_v4())).expect("get").is_none());
        let many = block_on(repo.get_many(&[inserted.id, Uuid::new_v4()])).expect("get_many");
        assert_eq!(
            many.iter().map(|doc| doc.id).collect::<Vec<_>>(),
            vec![inserted.id]
        );
        assert_eq!(
            block_on(repo.find_by_hash(&[7u8; 32], "test"))
                .expect("find")
                .map(|doc| doc.id),
            Some(inserted.id)
        );
        assert!(block_on(repo.find_by_hash(&[8u8; 32], "test"))
            .expect("find")
            .is_none());
        let mut patch = Map::new();
        patch.insert("author".to_owned(), Value::String("Kittel".to_owned()));
        let updated = block_on(repo.update_metadata(inserted.id, patch)).expect("patch merges");
        assert_eq!(
            updated.metadata.get("author"),
            Some(&Value::String("Kittel".to_owned()))
        );
        assert!(block_on(repo.update_metadata(Uuid::new_v4(), Map::new())).is_err());
        assert_eq!(
            block_on(repo.iter_by_filter(&DocumentFilter::default()))
                .expect("iter")
                .len(),
            1
        );
        block_on(repo.delete(inserted.id)).expect("delete removes");
        assert_eq!(block_on(repo.count(None)).expect("count"), 0);
        assert!(block_on(repo.get(inserted.id)).expect("get").is_none());
    }

    #[test]
    fn test_fake_passages_round_trip_every_method() {
        let repo = FakePassages {
            regions: vec![(10, 60)],
            stored: Mutex::new(HashMap::new()),
            embeddings: Mutex::new(HashMap::new()),
            fail_covering: false,
        };
        let id = doc_id();
        let covering = block_on(repo.covering_span(id, 10, 60)).expect("covering");
        assert_eq!(covering.len(), 1);
        assert_eq!(block_on(repo.count()).expect("count"), 0);
        let draft = |position: i64, start: i64, end: i64, text: &str| PassageDraft {
            position,
            char_start: start,
            char_end: end,
            locator: Map::new(),
            text: text.to_owned(),
            token_count: None,
            chunker: "test".to_owned(),
            chunker_version: "1".to_owned(),
            metadata: Map::new(),
            node_id: None,
        };
        let rows = block_on(repo.insert_many(
            &mut (),
            id,
            vec![
                draft(0, 0, 10, "alpha beta"),
                draft(1, 10, 20, "alpha alpha"),
                draft(2, 20, 30, "gamma"),
            ],
        ))
        .expect("insert stores");
        assert_eq!(rows.len(), 3);
        assert_eq!(block_on(repo.count()).expect("count"), 3);
        assert_eq!(
            block_on(repo.get(rows[0].id))
                .expect("get")
                .expect("present")
                .text,
            "alpha beta"
        );
        assert!(block_on(repo.get(Uuid::new_v4())).expect("get").is_none());
        let by_doc = block_on(repo.get_by_document(id)).expect("by document");
        assert_eq!(
            by_doc.iter().map(|row| row.position).collect::<Vec<_>>(),
            vec![0, 1, 2]
        );
        assert!(block_on(repo.get_by_document(Uuid::new_v4()))
            .expect("by document")
            .is_empty());
        let (before, center, after) =
            block_on(repo.get_context(rows[1].id, 1, 1)).expect("context");
        assert_eq!(center.id, rows[1].id);
        assert_eq!(
            before.iter().map(|row| row.id).collect::<Vec<_>>(),
            vec![rows[0].id]
        );
        assert_eq!(
            after.iter().map(|row| row.id).collect::<Vec<_>>(),
            vec![rows[2].id]
        );
        assert!(block_on(repo.get_context(Uuid::new_v4(), 1, 1)).is_err());
        // Embeddings round-trip and rank the vector search.
        block_on(repo.store_embeddings(
            &mut (),
            &[rows[0].id, rows[1].id, rows[2].id],
            &[vec![1.0, 0.0], vec![0.0, 1.0], vec![0.0, 0.0]],
            "m",
            "v",
            2,
        ))
        .expect("embeddings store");
        assert_eq!(
            block_on(repo.get_embedding(rows[0].id, "m", "v")).expect("embedding"),
            Some(vec![1.0, 0.0])
        );
        assert_eq!(
            block_on(repo.get_embedding(rows[0].id, "m", "other")).expect("embedding"),
            None
        );
        let ranked = block_on(repo.vector_search(&[1.0, 0.0], "m", "v", None, 10)).expect("search");
        assert_eq!(ranked[0].0, rows[0].id);
        let narrowed = block_on(repo.vector_search(&[1.0, 0.0], "m", "v", Some(&[rows[1].id]), 10))
            .expect("search");
        assert_eq!(
            narrowed.iter().map(|(id, _)| *id).collect::<Vec<_>>(),
            vec![rows[1].id]
        );
        assert_eq!(
            block_on(repo.vector_search(&[1.0, 0.0], "m", "v", None, 1))
                .expect("search")
                .len(),
            1
        );
        // Keyword search counts occurrences; shape errors fail loudly.
        let keyed = block_on(repo.keyword_search("alpha", None, None, 2)).expect("search");
        assert_eq!(
            keyed.iter().map(|(id, _)| *id).collect::<Vec<_>>(),
            vec![rows[1].id, rows[0].id]
        );
        // A candidate list narrows the rows before scoring them.
        let narrowed =
            block_on(repo.keyword_search("alpha", None, Some(&[rows[0].id]), 10)).expect("search");
        assert_eq!(
            narrowed.iter().map(|(id, _)| *id).collect::<Vec<_>>(),
            vec![rows[0].id]
        );
        assert!(block_on(repo.index_fts(&mut (), &[rows[0].id], &["t".to_owned()], "en")).is_ok());
        assert!(block_on(repo.index_fts(&mut (), &[rows[0].id], &[] as &[String], "en")).is_err());
        // Shape errors fail loudly: length mismatch, then unknown row.
        assert!(block_on(repo.store_embeddings(
            &mut (),
            &[rows[0].id],
            &[] as &[Vec<f64>],
            "m",
            "v",
            2
        ))
        .is_err());
        assert!(block_on(repo.store_embeddings(
            &mut (),
            &[Uuid::new_v4()],
            &[vec![1.0]],
            "m",
            "v",
            1
        ))
        .is_err());
        // The stub's fixed outputs are its contract; the fake ignores them.
        let stub = FilterExtensionStub;
        assert_eq!(stub.filter_id(), "stub");
        assert_eq!(stub.description(), "stub");
        assert!(stub.input_schema().is_empty());
        assert_eq!(
            stub.build_clause(&Value::Null).expect("stub builds"),
            "stub"
        );
        assert_eq!(
            block_on(repo.filter_candidate_ids::<FilterExtensionStub>(&Map::new(), None))
                .expect("filter")
                .len(),
            3
        );
    }

    /// No-op filter extension: the fake ignores filters, so any type works.
    struct FilterExtensionStub;
    impl FilterExtension for FilterExtensionStub {
        type Clause = String;
        fn filter_id(&self) -> &str {
            "stub"
        }
        fn input_schema(&self) -> Map<String, Value> {
            Map::new()
        }
        fn description(&self) -> &str {
            "stub"
        }
        fn build_clause(&self, _value: &Value) -> Result<String> {
            Ok("stub".to_owned())
        }
    }

    #[test]
    fn test_fake_claims_round_trip_every_method() {
        use marginalia_types::claims::{
            AnchorDraft, AnchorRole, AnchorVerifyStatus, ClaimDraft, ClaimKind, ClaimRelation,
            ClaimStatus,
        };
        let repo = FakeClaims {
            existing: Mutex::new(HashSet::new()),
            calls: Mutex::new(Vec::new()),
            claims: Mutex::new(HashMap::new()),
            anchors: Mutex::new(HashMap::new()),
            edges: Mutex::new(Vec::new()),
            fail_refs: false,
        };
        let draft = |statement: &str| ClaimDraft {
            r#ref: "T-1".to_owned(),
            statement: statement.to_owned(),
            kind: ClaimKind::Mine,
            status: ClaimStatus::Open,
            confidence: None,
            steelman: None,
            public_ready: false,
            academic_candidate: false,
            attributes: Map::new(),
        };
        let claim = block_on(repo.upsert_claim(&mut (), draft("first"))).expect("upsert inserts");
        // The upsert registers the ref, so the ledger check sees it.
        assert_eq!(
            block_on(repo.existing_refs(&["T-1".to_owned(), "NOPE".to_owned()])).expect("refs"),
            HashSet::from(["T-1".to_owned()])
        );
        let again = block_on(repo.upsert_claim(&mut (), draft("second"))).expect("upsert updates");
        assert_eq!(again.id, claim.id);
        assert_eq!(
            block_on(repo.get_by_ref("T-1"))
                .expect("get")
                .expect("present")
                .statement,
            "second"
        );
        let anchor = block_on(repo.add_anchor(
            &mut (),
            claim.id,
            AnchorDraft {
                role: AnchorRole::Supports,
                quoted_text: "words".to_owned(),
                source_span_id: Uuid::new_v4(),
                person_entity_id: None,
                verify_status: AnchorVerifyStatus::Exact,
                verified_at: chrono::Utc::now(),
                parser_version: None,
                edition_id: None,
                edition_key: None,
                locator: Map::new(),
            },
        ))
        .expect("anchor stores");
        assert_eq!(
            block_on(repo.anchor_by_id(anchor.id))
                .expect("by id")
                .expect("present")
                .id,
            anchor.id
        );
        assert!(block_on(repo.anchor_by_id(Uuid::new_v4()))
            .expect("by id")
            .is_none());
        assert_eq!(block_on(repo.anchors_for(claim.id)).expect("for").len(), 1);
        assert!(block_on(repo.anchors_for(Uuid::new_v4()))
            .expect("for")
            .is_empty());
        let other = block_on(repo.upsert_claim(
            &mut (),
            ClaimDraft {
                r#ref: "T-2".to_owned(),
                statement: "other".to_owned(),
                kind: ClaimKind::Ally,
                status: ClaimStatus::Open,
                confidence: Some(0.9),
                steelman: None,
                public_ready: false,
                academic_candidate: true,
                attributes: Map::new(),
            },
        ))
        .expect("second claim inserts");
        block_on(repo.add_edge(
            &mut (),
            claim.id,
            other.id,
            ClaimRelation::Supports,
            Some(0.9),
            Some("holds"),
        ))
        .expect("edge stores");
        // `edges_for` sees both directions of the same edge.
        assert_eq!(block_on(repo.edges_for(claim.id)).expect("edges").len(), 1);
        assert_eq!(block_on(repo.edges_for(other.id)).expect("edges").len(), 1);
        assert!(block_on(repo.edges_for(Uuid::new_v4()))
            .expect("edges")
            .is_empty());
        let audit = block_on(repo.audit(Some(&["T-1".to_owned()]))).expect("audit");
        assert_eq!(audit.checked_refs, Some(vec!["T-1".to_owned()]));
        assert!(!audit.assurance.is_empty());
        assert_eq!(
            block_on(repo.audit(None)).expect("audit").checked_refs,
            None
        );
    }
}
