//! Validate a revision's rows — the Phase-1 `work_validate`.
//!
//! Python source: `services/works/validate.py`. Findings carry rule ids;
//! severities are the core floor, moved by the per-work-type policy between
//! error, warn, and allow. At gate `none` the report lists and passes; at
//! `freeze` and `publish` every unwaived error blocks, and waivers clear the
//! finding they name.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use marginalia_types::citations::CitationItem;
use marginalia_types::documents::Document;
use marginalia_types::ports::{
    CitationRepo, DocumentRepo, DocumentTextRepo, EditionRepo, PassageRepo, SourceSpanRepo,
    WaiverRepo, WorkBlockRepo, WorkLinkRepo, WorkRepo, WorkRevisionRepo,
};
use marginalia_types::spans::SourceSpan;
use marginalia_types::works::{RevisionState, Waiver, Work, WorkRevision};
use marginalia_types::works_ports::DraftExporter;
use marginalia_types::{Error, Result};

use crate::assembly::{assemble_revision, hash_assembled, AssembledRevision};
use crate::markers::find_markers;
use crate::publication::{ValidationGateReport, ValidationPort};
use crate::py_repr_str;
use crate::verify::{intent_value, MAX_QUOTE_CHARS};

/// Validation gates. Unlike `verify`'s review gate, revisions freeze.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ValidateGate {
    None,
    Freeze,
    Publish,
}

impl ValidateGate {
    pub fn parse(name: &str) -> Result<Self, Error> {
        match name {
            "none" => Ok(Self::None),
            "freeze" => Ok(Self::Freeze),
            "publish" => Ok(Self::Publish),
            _ => Err(Error::Validation(format!(
                "Unknown gate {}",
                py_repr_str(name)
            ))),
        }
    }

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Freeze => "freeze",
            Self::Publish => "publish",
        }
    }
}

impl fmt::Display for ValidateGate {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// Intents that must cite a narrowed span, never a whole passage.
pub fn is_narrow_intent(intent: &str) -> bool {
    matches!(intent, "quotation" | "translation")
}

/// Intents warned when they cite a whole passage.
pub fn is_region_warn_intent(intent: &str) -> bool {
    matches!(intent, "support" | "source" | "definition")
}

/// Block types that must lean on the corpus or say nothing.
pub fn is_grounded_type(block_type: &str) -> bool {
    matches!(block_type, "translation_unit" | "quotation")
}

/// The core floor: rule id to severity before the work-type policy moves it.
pub static DEFAULT_SEVERITY: &[(&str, &str)] = &[
    ("AUTH_DOCUMENT_UNKNOWN", "error"),
    ("AUTH_SOURCE_UNCHECKABLE", "error"),
    ("AUTH_QUOTE_UNVERIFIED", "error"),
    ("AUTH_SOURCE_SPAN_STALE", "error"),
    ("AUTH_SPAN_NOT_NARROWED", "error"),
    ("AUTH_SPAN_REGION", "warning"),
    ("AUTH_CITATION_EDITION_MISSING", "warning"),
    ("AUTH_CITATION_EDITION_MISMATCH", "error"),
    ("AUTH_EDITION_KEY_UNKNOWN", "warning"),
    ("AUTH_CITATION_MARKER_MISSING", "error"),
    ("AUTH_CITATION_MARKER_DANGLING", "error"),
    ("AUTH_PARENT_REVISION_MISMATCH", "error"),
    ("AUTH_REVISION_MUTATED", "error"),
    ("AUTH_BIBLIOGRAPHY_ONLY", "warning"),
    ("AUTH_BLOCK_UNGROUNDED", "warning"),
    ("AUTH_UNUSED_CITATION", "warning"),
    ("AUTH_FILE_DRIFT", "warning"),
    ("AUTH_LICENSE_EXPORT", "warning"),
];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ValidationFinding {
    pub rule_id: String,
    pub severity: String,
    #[serde(default)]
    pub block_key: Option<String>,
    #[serde(default)]
    pub citation_key: Option<String>,
    pub message: String,
    #[serde(default)]
    pub detail: Option<Map<String, Value>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CitationCheck {
    pub citation_key: String,
    pub block_key: String,
    pub intent: String,
    #[serde(default)]
    pub tier: Option<String>,
    #[serde(default)]
    pub char_start: Option<i64>,
    #[serde(default)]
    pub char_end: Option<i64>,
    #[serde(default)]
    pub findings: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GateResult {
    pub name: ValidateGate,
    pub passed: bool,
    #[serde(default)]
    pub blockers: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ValidationReport {
    pub work: String,
    pub revision_number: i64,
    pub state: String,
    #[serde(default)]
    pub citations: Vec<CitationCheck>,
    #[serde(default)]
    pub findings: Vec<ValidationFinding>,
    pub gate: GateResult,
}

/// The work-type policy moves a rule between error, warn, and allow.
///
/// Unknown values fall back to the core floor: a typo must not silence a
/// check, and `allow` is the only way to drop a finding.
pub fn resolve_severity(
    policy: &HashMap<String, HashMap<String, String>>,
    work_type: &str,
    rule_id: &str,
    default: &str,
) -> String {
    match policy
        .get(work_type)
        .and_then(|rules| rules.get(rule_id))
        .map(String::as_str)
    {
        Some("allow") => "allow".to_owned(),
        Some("error") => "error".to_owned(),
        Some("warning") | Some("warn") => "warning".to_owned(),
        _ => default.to_owned(),
    }
}

/// Rule ids that fail the gate: unwaived errors, plus edition identity at
/// publish. `allow`-severity findings never block.
pub fn validation_blockers(
    findings: &[ValidationFinding],
    gate: ValidateGate,
    waived: &HashSet<(String, Option<String>)>,
) -> Vec<String> {
    if gate == ValidateGate::None {
        return Vec::new();
    }
    let mut blockers = HashSet::new();
    for finding in findings {
        if finding.severity != "error" && finding.severity != "warning" {
            continue;
        }
        let subject = finding
            .citation_key
            .clone()
            .or_else(|| finding.block_key.clone());
        if waived.contains(&(finding.rule_id.clone(), subject)) {
            continue;
        }
        if waived.contains(&(finding.rule_id.clone(), None)) {
            continue;
        }
        if finding.severity == "error"
            || (gate == ValidateGate::Publish && finding.rule_id == "AUTH_CITATION_EDITION_MISSING")
        {
            blockers.insert(finding.rule_id.clone());
        }
    }
    let mut sorted: Vec<String> = blockers.into_iter().collect();
    sorted.sort();
    sorted
}

/// Per-citation roll-up: findings grouped by citation key over the view's
/// occurrences, with the first span bounds and first verify status.
pub fn citation_checks(
    view: &AssembledRevision,
    findings: &[ValidationFinding],
) -> Vec<CitationCheck> {
    let mut by_citation: HashMap<&str, Vec<String>> = HashMap::new();
    for finding in findings {
        if let Some(key) = finding.citation_key.as_deref() {
            by_citation
                .entry(key)
                .or_default()
                .push(finding.rule_id.clone());
        }
    }
    let mut checks = Vec::new();
    for item in &view.blocks {
        for entry in &item.citations {
            let key = entry.occurrence.citation_key.to_string();
            // First span in view order: the first item whose span survived
            // assembly, exactly as the Python `next(...)` reads.
            let first_span = entry
                .items
                .iter()
                .filter_map(|row| row.source_span_id)
                .filter_map(|span_id| view.spans.get(&span_id))
                .next();
            let tier = entry.items.iter().find_map(|row| {
                row.verify_status
                    .as_ref()
                    .filter(|status| !status.is_empty())
                    .cloned()
            });
            checks.push(CitationCheck {
                citation_key: key.clone(),
                block_key: item.block.block_key.to_string(),
                intent: intent_value(&entry.occurrence.intent).to_owned(),
                tier,
                char_start: first_span.map(|span| span.char_start),
                char_end: first_span.map(|span| span.char_end),
                findings: by_citation.get(key.as_str()).cloned().unwrap_or_default(),
            });
        }
    }
    checks
}

/// `RevisionState.value`: the revision lifecycle, as rows spell it.
fn revision_state_value(state: &RevisionState) -> &'static str {
    match state {
        RevisionState::Draft => "draft",
        RevisionState::Frozen => "frozen",
        RevisionState::Published => "published",
        RevisionState::Superseded => "superseded",
    }
}

/// Move every finding to its policy severity, with the license two-stage:
/// the gate sets the default, then error wins over warning. `allow` drops
/// the finding — except `AUTH_LICENSE_EXPORT`, where the gate floor holds
/// and a policy `allow` degrades to a warning instead of silence.
fn apply_policy(
    findings: &mut Vec<ValidationFinding>,
    policy: &HashMap<String, HashMap<String, String>>,
    work_type: &str,
    gate: ValidateGate,
) {
    for finding in findings.iter_mut() {
        let mut default = finding.severity.clone();
        if finding.rule_id == "AUTH_LICENSE_EXPORT" {
            default = if gate == ValidateGate::Publish {
                "error"
            } else {
                "warning"
            }
            .to_owned();
        }
        let configured = resolve_severity(policy, work_type, &finding.rule_id, &default);
        if finding.rule_id == "AUTH_LICENSE_EXPORT" {
            finding.severity = if default == "error" || configured == "error" {
                "error"
            } else {
                "warning"
            }
            .to_owned();
        } else {
            finding.severity = configured;
        }
    }
    findings.retain(|finding| finding.severity == "error" || finding.severity == "warning");
}

fn waiver_index(rows: &[Waiver]) -> HashSet<(String, Option<String>)> {
    rows.iter()
        .map(|row| (row.rule_id.clone(), row.subject.clone()))
        .collect()
}

/// Flag the findings a stored or prospective waiver clears. Only the exact
/// `(rule, subject)` pair flags; the global `(rule, None)` pair clears the
/// blocker without annotating every row, exactly as Python does.
fn mark_waived(findings: &mut [ValidationFinding], waived: &HashSet<(String, Option<String>)>) {
    for finding in findings.iter_mut() {
        let subject = finding
            .citation_key
            .clone()
            .or_else(|| finding.block_key.clone());
        if waived.contains(&(finding.rule_id.clone(), subject)) {
            let mut detail = finding.detail.clone().unwrap_or_default();
            detail.insert("waived".to_owned(), Value::Bool(true));
            finding.detail = Some(detail);
        }
    }
}

/// Walk the parent chain from the latest revision to the requested number,
/// mirroring `_resolve_revision`.
async fn resolve_revision_in_chain<R: WorkRevisionRepo>(
    revisions: &R,
    work: &Work,
    revision: Option<i64>,
) -> Result<WorkRevision> {
    match revision {
        None => {
            let Some(current_id) = work.current_revision_id else {
                return Err(Error::NotFound {
                    kind: "work_revision",
                    id: format!("current of {}", work.slug),
                });
            };
            revisions
                .get(current_id)
                .await?
                .ok_or_else(|| Error::NotFound {
                    kind: "work_revision",
                    id: current_id.to_string(),
                })
        }
        Some(number) => {
            let latest = revisions
                .latest(work.id)
                .await?
                .ok_or_else(|| Error::NotFound {
                    kind: "work_revision",
                    id: format!("{} revision 1", work.slug),
                })?;
            let mut current = latest;
            while current.revision_number != number {
                let Some(parent_id) = current.parent_revision_id else {
                    return Err(Error::NotFound {
                        kind: "work_revision",
                        id: format!("{} revision {number}", work.slug),
                    });
                };
                current = revisions
                    .get(parent_id)
                    .await?
                    .ok_or_else(|| Error::NotFound {
                        kind: "work_revision",
                        id: format!("{} revision {number}", work.slug),
                    })?;
            }
            Ok(current)
        }
    }
}

/// A flipped work whose file moved since its last export drifts. The flip
/// records its file under `metadata.port.file`; until flip code exists that
/// key is absent and the check is skipped, never failed.
async fn check_drift_file<X: DraftExporter>(
    works_dir: &Path,
    exporter: &X,
    work: &Work,
    view: &AssembledRevision,
) -> Result<Option<ValidationFinding>> {
    let rel = view
        .revision
        .metadata
        .get("port")
        .and_then(Value::as_object)
        .and_then(|port| port.get("file"))
        .and_then(Value::as_str)
        .filter(|rel| !rel.is_empty());
    let Some(rel) = rel else {
        return Ok(None);
    };
    let path = works_dir.join(rel);
    if !path.is_file() {
        return Ok(None);
    }
    let rendered = exporter.export_markdown(work.id).await?;
    let on_disk = std::fs::read_to_string(&path).map_err(|error| {
        Error::Storage(format!("cannot read work file {}: {error}", path.display()))
    })?;
    if on_disk != rendered {
        Ok(Some(ValidationFinding {
            rule_id: "AUTH_FILE_DRIFT".to_owned(),
            severity: "warning".to_owned(),
            block_key: None,
            citation_key: None,
            message: format!("File {rel} differs from the last export of {}", work.slug),
            detail: None,
        }))
    } else {
        Ok(None)
    }
}

/// Structural and corpus checks over one revision, keyed by block and cite.
pub struct WorkValidationService<W, R, B, C, L, E, Wv, S, D, T, P, X> {
    works: W,
    revisions: R,
    blocks: B,
    citations: C,
    links: L,
    editions: E,
    waivers: Wv,
    spans: S,
    documents: D,
    texts: T,
    passages: P,
    policy: HashMap<String, HashMap<String, String>>,
    works_dir: Option<PathBuf>,
    exporter: Option<X>,
}

impl<W, R, B, C, L, E, Wv, S, D, T, P, X> WorkValidationService<W, R, B, C, L, E, Wv, S, D, T, P, X>
where
    W: WorkRepo,
    R: WorkRevisionRepo,
    B: WorkBlockRepo,
    C: CitationRepo,
    L: WorkLinkRepo,
    E: EditionRepo,
    Wv: WaiverRepo,
    S: SourceSpanRepo,
    D: DocumentRepo,
    T: DocumentTextRepo,
    P: PassageRepo,
    X: DraftExporter,
{
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        works: W,
        revisions: R,
        blocks: B,
        citations: C,
        links: L,
        editions: E,
        waivers: Wv,
        spans: S,
        documents: D,
        texts: T,
        passages: P,
        policy: HashMap<String, HashMap<String, String>>,
        works_dir: Option<PathBuf>,
        exporter: Option<X>,
    ) -> Self {
        Self {
            works,
            revisions,
            blocks,
            citations,
            links,
            editions,
            waivers,
            spans,
            documents,
            texts,
            passages,
            policy,
            works_dir,
            exporter,
        }
    }

    /// Check a revision and judge it against the gate.
    ///
    /// `prospective` waivers clear blockers like stored rows, so the freeze
    /// call can pass on the call that earns its waivers.
    pub async fn validate(
        &self,
        slug: &str,
        revision: Option<i64>,
        gate: ValidateGate,
        prospective: Option<&HashSet<(String, Option<String>)>>,
    ) -> Result<ValidationReport> {
        let work = self
            .works
            .get_by_slug(slug)
            .await?
            .ok_or_else(|| Error::NotFound {
                kind: "work",
                id: slug.to_owned(),
            })?;
        let resolved = self.resolve_revision_number(&work, revision).await?;
        let view = assemble_revision(
            &work,
            &resolved,
            &self.blocks,
            &self.citations,
            &self.links,
            &self.spans,
        )
        .await?;
        let mut checker = RevisionChecker::new(
            &view,
            &self.editions,
            &self.documents,
            &self.texts,
            &self.passages,
        );
        let mut findings = checker.run().await?;
        if self.works_dir.is_some() && self.exporter.is_some() && revision.is_none() {
            if let Some(drift) = self.check_drift(&work, &view).await? {
                findings.push(drift);
            }
        }
        apply_policy(&mut findings, &self.policy, &work.work_type, gate);
        let mut waived = waiver_index(&self.waivers.for_revision(resolved.id).await?);
        if let Some(extra) = prospective {
            waived.extend(extra.iter().cloned());
        }
        let blockers = validation_blockers(&findings, gate, &waived);
        mark_waived(&mut findings, &waived);
        let citations = citation_checks(&view, &findings);
        Ok(ValidationReport {
            work: work.slug.clone(),
            revision_number: resolved.revision_number,
            state: revision_state_value(&resolved.state).to_owned(),
            citations,
            findings,
            gate: GateResult {
                name: gate,
                passed: gate == ValidateGate::None || blockers.is_empty(),
                blockers,
            },
        })
    }

    async fn check_drift(
        &self,
        work: &Work,
        view: &AssembledRevision,
    ) -> Result<Option<ValidationFinding>> {
        match (&self.works_dir, &self.exporter) {
            (Some(dir), Some(exporter)) => check_drift_file(dir, exporter, work, view).await,
            _ => Ok(None),
        }
    }

    pub async fn resolve_revision_number(
        &self,
        work: &Work,
        revision: Option<i64>,
    ) -> Result<WorkRevision> {
        resolve_revision_in_chain(&self.revisions, work, revision).await
    }
}

impl<W, R, B, C, L, E, Wv, S, D, T, P, X> ValidationPort
    for WorkValidationService<W, R, B, C, L, E, Wv, S, D, T, P, X>
where
    W: WorkRepo,
    R: WorkRevisionRepo,
    B: WorkBlockRepo,
    C: CitationRepo,
    L: WorkLinkRepo,
    E: EditionRepo,
    Wv: WaiverRepo,
    S: SourceSpanRepo,
    D: DocumentRepo,
    T: DocumentTextRepo,
    P: PassageRepo,
    X: DraftExporter,
{
    async fn validate_for_gate(
        &self,
        slug: &str,
        gate: ValidateGate,
        prospective: &HashSet<(String, Option<String>)>,
    ) -> Result<ValidationGateReport> {
        let report = self.validate(slug, None, gate, Some(prospective)).await?;
        Ok(ValidationGateReport {
            passed: report.gate.passed,
            blockers: report.gate.blockers,
        })
    }
}

/// The per-revision checks, holding the read caches. Generic over the four
/// corpus reads; the assembled view carries the rest.
///
/// The checker borrows its repos — like the Python `_Checker`, which holds
/// references — so `validate` can build it from `&self` without cloning.
pub struct RevisionChecker<'view, E, D, T, P> {
    view: &'view AssembledRevision,
    editions: &'view E,
    documents: &'view D,
    texts: &'view T,
    passages: &'view P,
    findings: Vec<ValidationFinding>,
    documents_cache: HashMap<String, Document>,
    edition_keys: Option<HashSet<String>>,
    parser_versions: HashMap<String, String>,
}

impl<'view, E, D, T, P> RevisionChecker<'view, E, D, T, P>
where
    E: EditionRepo,
    D: DocumentRepo,
    T: DocumentTextRepo,
    P: PassageRepo,
{
    pub fn new(
        view: &'view AssembledRevision,
        editions: &'view E,
        documents: &'view D,
        texts: &'view T,
        passages: &'view P,
    ) -> Self {
        Self {
            view,
            editions,
            documents,
            texts,
            passages,
            findings: Vec::new(),
            documents_cache: HashMap::new(),
            edition_keys: None,
            parser_versions: HashMap::new(),
        }
    }

    pub async fn run(&mut self) -> Result<Vec<ValidationFinding>> {
        self.prime_corpus_caches().await?;
        // `view` is a shared reference, `Copy` out of the `&mut self`
        // borrow so marker and block walks can record findings as they go.
        let view = self.view;
        let mut markers: HashMap<String, (HashSet<String>, Vec<String>)> = HashMap::new();
        for item in &view.blocks {
            markers.insert(
                item.block.block_key.to_string(),
                find_markers(&item.block.body_markdown),
            );
        }
        for item in &view.blocks {
            let block_key = item.block.block_key.to_string();
            // Proof: every view block inserted its marker row above.
            let (_, invalid) = markers.get(&block_key).expect("marker row per block");
            for raw in invalid {
                let mut detail = Map::new();
                detail.insert("marker".to_owned(), Value::String(raw.clone()));
                self.add(
                    "AUTH_CITATION_MARKER_DANGLING",
                    "error",
                    Some(block_key.clone()),
                    None,
                    format!("Marker {raw} names no citation: keys are UUIDs"),
                    Some(detail),
                );
            }
        }
        let mut occurrence_blocks: HashMap<String, String> = HashMap::new();
        for item in &view.blocks {
            for entry in &item.citations {
                occurrence_blocks.insert(
                    entry.occurrence.citation_key.to_string(),
                    item.block.block_key.to_string(),
                );
            }
        }
        for item in &view.blocks {
            let block_key = item.block.block_key.to_string();
            // Proof: every view block inserted its marker row above.
            let (keys, _) = markers.get(&block_key).expect("marker row per block");
            for entry in &item.citations {
                let citation_key = entry.occurrence.citation_key.to_string();
                if !keys.contains(&citation_key) {
                    self.add(
                        "AUTH_CITATION_MARKER_MISSING",
                        "error",
                        Some(block_key.clone()),
                        Some(citation_key.clone()),
                        format!(
                            "Occurrence {citation_key} has no {{{{cite:…}}}} marker in its block"
                        ),
                        None,
                    );
                }
                for row in &entry.items {
                    self.check_item(
                        &block_key,
                        &citation_key,
                        intent_value(&entry.occurrence.intent),
                        row,
                    )
                    .await?;
                }
            }
            // Deterministic order: the Python set walk is stable per
            // process only, so tests assert membership, never sequence.
            let mut ordered: Vec<&String> = keys.iter().collect();
            ordered.sort();
            for key in ordered {
                match occurrence_blocks.get(key) {
                    None => {
                        let mut detail = Map::new();
                        detail.insert("citation_key".to_owned(), Value::String(key.clone()));
                        self.add(
                            "AUTH_CITATION_MARKER_DANGLING",
                            "error",
                            Some(block_key.clone()),
                            None,
                            format!(
                                "Marker {{{{cite:{key}}}}} matches no occurrence on this block"
                            ),
                            Some(detail),
                        );
                    }
                    Some(home) if *home != block_key => {
                        self.add(
                            "AUTH_UNUSED_CITATION",
                            "warning",
                            Some(home.clone()),
                            Some(key.clone()),
                            format!(
                                "Occurrence {key} is marked in another block, not the one rendered with it"
                            ),
                            None,
                        );
                    }
                    Some(_) => {}
                }
            }
            self.check_block(item);
        }
        self.check_license_quota();
        self.check_revision();
        Ok(std::mem::take(&mut self.findings))
    }

    async fn prime_corpus_caches(&mut self) -> Result<()> {
        let doc_ids: HashSet<Uuid> = self
            .view
            .spans
            .values()
            .map(|span| span.document_id)
            .collect();
        for doc_id in &doc_ids {
            if let Some(document) = self.documents.get(*doc_id).await? {
                self.documents_cache.insert(doc_id.to_string(), document);
            }
        }
        if !doc_ids.is_empty() {
            let ids: Vec<Uuid> = doc_ids.into_iter().collect();
            let versions = self.texts.parser_versions(&ids).await?;
            self.parser_versions = versions
                .into_iter()
                .map(|(id, version)| (id.to_string(), version))
                .collect();
        }
        self.edition_keys = Some(self.editions.list_keys().await?.into_iter().collect());
        Ok(())
    }

    async fn check_item(
        &mut self,
        block_key: &str,
        citation_key: &str,
        intent: &str,
        row: &CitationItem,
    ) -> Result<()> {
        if row.edition_id.is_none() && row.edition_key.is_none() {
            self.add(
                "AUTH_CITATION_EDITION_MISSING",
                "warning",
                Some(block_key.to_owned()),
                Some(citation_key.to_owned()),
                "The citation names no edition: edition_key or edition_id".to_owned(),
                None,
            );
        }
        if let Some(key) = row.edition_key.as_ref() {
            // Proof: `prime_corpus_caches` runs before any item check.
            let known = self.edition_keys.as_ref().expect("primed edition keys");
            if !known.contains(key) {
                self.add(
                    "AUTH_EDITION_KEY_UNKNOWN",
                    "warning",
                    Some(block_key.to_owned()),
                    Some(citation_key.to_owned()),
                    format!("edition_key {key} has no bibliography.editions row"),
                    None,
                );
            }
        }
        let Some(span_id) = row.source_span_id else {
            self.add(
                "AUTH_BIBLIOGRAPHY_ONLY",
                "warning",
                Some(block_key.to_owned()),
                Some(citation_key.to_owned()),
                "The citation has identity but no span: bibliography, not evidence".to_owned(),
                None,
            );
            return Ok(());
        };
        // A span id no view carries is unreachable: the assembly join
        // restricts spans under their rows, so silence — not a finding —
        // is exact.
        let view = self.view;
        let Some(span) = view.spans.get(&span_id) else {
            return Ok(());
        };
        let Some(document) = self
            .documents_cache
            .get(&span.document_id.to_string())
            .cloned()
        else {
            self.add(
                "AUTH_DOCUMENT_UNKNOWN",
                "error",
                Some(block_key.to_owned()),
                Some(citation_key.to_owned()),
                format!("Document {} does not exist", span.document_id),
                None,
            );
            return Ok(());
        };
        let Some(document_version) = self
            .parser_versions
            .get(&span.document_id.to_string())
            .cloned()
        else {
            self.add(
                "AUTH_SOURCE_UNCHECKABLE",
                "error",
                Some(block_key.to_owned()),
                Some(citation_key.to_owned()),
                format!("Document {} has no canonical text", span.document_id),
                None,
            );
            return Ok(());
        };
        if span.parser_version.as_deref() != Some(document_version.as_str()) {
            let mut detail = Map::new();
            detail.insert(
                "span_parser_version".to_owned(),
                span.parser_version
                    .clone()
                    .map_or(Value::Null, Value::String),
            );
            detail.insert(
                "document_parser_version".to_owned(),
                Value::String(document_version),
            );
            self.add(
                "AUTH_SOURCE_SPAN_STALE",
                "error",
                Some(block_key.to_owned()),
                Some(citation_key.to_owned()),
                "The document was re-parsed since this span resolved: re-verify it".to_owned(),
                Some(detail),
            );
        }
        self.check_identity(block_key, citation_key, &document, row)
            .await?;
        if !matches!(
            row.verify_status.as_deref(),
            Some("exact") | Some("normalized")
        ) {
            let mut detail = Map::new();
            detail.insert(
                "verify_status".to_owned(),
                row.verify_status.clone().map_or(Value::Null, Value::String),
            );
            self.add(
                "AUTH_QUOTE_UNVERIFIED",
                "error",
                Some(block_key.to_owned()),
                Some(citation_key.to_owned()),
                format!(
                    "Quote verifies {}, not exact or normalized: earn a waiver or re-anchor it",
                    row.verify_status.as_deref().unwrap_or("None")
                ),
                Some(detail),
            );
        }
        let region = self.is_region(span).await?;
        let length = span.char_end - span.char_start;
        if is_narrow_intent(intent) && (region || length > MAX_QUOTE_CHARS) {
            self.add(
                "AUTH_SPAN_NOT_NARROWED",
                "error",
                Some(block_key.to_owned()),
                Some(citation_key.to_owned()),
                "A quotation or translation must cite a narrowed span, not a whole passage"
                    .to_owned(),
                None,
            );
        } else if is_region_warn_intent(intent) && region {
            self.add(
                "AUTH_SPAN_REGION",
                "warning",
                Some(block_key.to_owned()),
                Some(citation_key.to_owned()),
                "This span is exactly one passage: narrow it if the point rests on less".to_owned(),
                None,
            );
        }
        Ok(())
    }

    async fn check_identity(
        &mut self,
        block_key: &str,
        citation_key: &str,
        document: &Document,
        row: &CitationItem,
    ) -> Result<()> {
        let mut item_key = row.edition_key.clone();
        if item_key.is_none() {
            if let Some(edition_id) = row.edition_id {
                let edition = self.editions.get(edition_id).await?;
                item_key = edition.map(|edition| edition.edition_key);
            }
        }
        let Some(item_key) = item_key else {
            return Ok(());
        };
        match document.metadata.get("edition_key") {
            None => self.add(
                "AUTH_EDITION_KEY_UNKNOWN",
                "warning",
                Some(block_key.to_owned()),
                Some(citation_key.to_owned()),
                format!("edition_key {item_key} is on no ingested document yet"),
                None,
            ),
            Some(Value::String(document_key)) if document_key == &item_key => {}
            Some(document_key) => {
                let mut detail = Map::new();
                detail.insert("item_key".to_owned(), Value::String(item_key.clone()));
                detail.insert("document_key".to_owned(), document_key.clone());
                self.add(
                    "AUTH_CITATION_EDITION_MISMATCH",
                    "error",
                    Some(block_key.to_owned()),
                    Some(citation_key.to_owned()),
                    format!(
                        "Citation key {item_key} differs from the span's document key {}",
                        metadata_scalar_text(document_key)
                    ),
                    Some(detail),
                );
            }
        }
        Ok(())
    }

    /// Proof: the body only walks the assembled view and records findings —
    /// no I/O, no `?`, no `Err` — so the `Result` carried a dead error arm.
    fn check_block(&mut self, item: &crate::assembly::AssembledBlock) {
        let block = &item.block;
        if is_grounded_type(&block.block_type) {
            let has_grounding = !item.citations.is_empty()
                || item
                    .links
                    .as_ref()
                    .is_some_and(|links| !links.sources.is_empty());
            if !has_grounding {
                self.add(
                    "AUTH_BLOCK_UNGROUNDED",
                    "warning",
                    Some(block.block_key.to_string()),
                    None,
                    format!(
                        "A {} block with no citation or source link leans on nothing",
                        block.block_type
                    ),
                    None,
                );
            }
        }
        if let Some(parent_id) = block.parent_id {
            let view = self.view;
            if !view.blocks.iter().any(|entry| entry.block.id == parent_id) {
                self.add(
                    "AUTH_PARENT_REVISION_MISMATCH",
                    "error",
                    Some(block.block_key.to_string()),
                    None,
                    "The block's parent is in another revision".to_owned(),
                    None,
                );
            }
        }
    }

    fn check_license_quota(&mut self) {
        // `view` is a shared reference: the totals walk borrows nothing
        // mutable, and findings record through `self.add`.
        let view = self.view;
        let mut totals: HashMap<Uuid, i64> = HashMap::new();
        let mut citation_keys: HashMap<Uuid, HashSet<String>> = HashMap::new();
        let mut seen_items: HashSet<(Uuid, i64)> = HashSet::new();
        for block in &view.blocks {
            for entry in &block.citations {
                let key = entry.occurrence.citation_key.to_string();
                for row in &entry.items {
                    let item_key = (row.occurrence_id, row.position);
                    // `len` counts characters the way Python's `len(str)`
                    // does: multibyte text quotes fewer bytes than it reads.
                    let Some(quoted) = row.quoted_text.as_ref() else {
                        continue;
                    };
                    if !seen_items.insert(item_key) {
                        continue;
                    }
                    let Some(span_id) = row.source_span_id else {
                        continue;
                    };
                    let Some(span) = view.spans.get(&span_id) else {
                        continue;
                    };
                    let length = quoted.chars().count() as i64;
                    *totals.entry(span.document_id).or_default() += length;
                    citation_keys
                        .entry(span.document_id)
                        .or_default()
                        .insert(key.clone());
                }
            }
        }
        let mut ordered: Vec<(Uuid, i64)> = totals.into_iter().collect();
        ordered.sort_by_key(|entry| entry.0);
        for (document_id, total) in ordered {
            if total <= MAX_QUOTE_CHARS {
                continue;
            }
            // Proof: only totals above the cap reach here, so every id has
            // at least one citing key recorded alongside its total.
            let mut keys: Vec<String> = citation_keys
                .get(&document_id)
                .expect("keys recorded with the total")
                .iter()
                .cloned()
                .collect();
            keys.sort();
            let mut detail = Map::new();
            detail.insert(
                "document_id".to_owned(),
                Value::String(document_id.to_string()),
            );
            detail.insert("quoted_characters".to_owned(), Value::from(total));
            detail.insert("cap".to_owned(), Value::from(MAX_QUOTE_CHARS));
            detail.insert(
                "citation_keys".to_owned(),
                Value::Array(keys.into_iter().map(Value::String).collect()),
            );
            self.add(
                "AUTH_LICENSE_EXPORT",
                "warning",
                None,
                None,
                format!(
                    "Stored quotations copy {total} characters from document {document_id}, above the {MAX_QUOTE_CHARS}-character cap"
                ),
                Some(detail),
            );
        }
    }

    /// Proof: the body only reads the view and records findings — no I/O,
    /// no `?`, no `Err` — so the `Result` carried a dead error arm.
    fn check_revision(&mut self) {
        let revision = &self.view.revision;
        if revision.state == RevisionState::Draft || revision.content_hash.is_none() {
            return;
        }
        if hash_assembled(self.view).as_slice()
            != revision.content_hash.as_deref().unwrap_or_default()
        {
            self.add(
                "AUTH_REVISION_MUTATED",
                "error",
                None,
                None,
                format!(
                    "Revision {} no longer matches its frozen hash",
                    revision.revision_number
                ),
                None,
            );
        }
    }

    async fn is_region(&self, span: &SourceSpan) -> Result<bool> {
        let covering = self
            .passages
            .covering_span(span.document_id, span.char_start, span.char_end)
            .await?;
        Ok(covering.iter().any(|passage| {
            passage.char_start == Some(span.char_start) && passage.char_end == Some(span.char_end)
        }))
    }

    fn add(
        &mut self,
        rule_id: &str,
        severity: &str,
        block_key: Option<String>,
        citation_key: Option<String>,
        message: String,
        detail: Option<Map<String, Value>>,
    ) {
        self.findings.push(ValidationFinding {
            rule_id: rule_id.to_owned(),
            severity: severity.to_owned(),
            block_key,
            citation_key,
            message,
            detail,
        });
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::future::Future;
    use std::sync::Mutex;

    use chrono::Utc;
    use marginalia_types::citations::{BlockCitations, CitationOccurrence};
    use marginalia_types::documents::{DocumentFilter, DocumentText};
    use marginalia_types::passages::{Passage, PassageDraft};
    use marginalia_types::ports::FilterExtension;
    use marginalia_types::works::{
        BlockLinks, BlockSourceLink, Edition, Placement, WorkBlock, WorkStatus,
    };
    use marginalia_types::works_files::Intent;
    use marginalia_types::{Error, Result};

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
        Uuid::from_u128(0x0123_4567_89ab_cdef_0123_4567_89ab_cdef)
    }

    fn block_uuid(n: u128) -> Uuid {
        Uuid::from_u128(n)
    }

    fn edition_metadata(key: &str) -> Map<String, Value> {
        let mut metadata = Map::new();
        metadata.insert("edition_key".to_owned(), Value::String(key.to_owned()));
        metadata
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
            parser_version: "pv2".to_owned(),
            ingested_at: Utc::now(),
            created_date_start: None,
            created_date_end: None,
            created_precision: None,
            edition_id: None,
            metadata,
        }
    }

    struct FakeDocs {
        docs: Mutex<HashMap<Uuid, Document>>,
        fail: bool,
    }

    /// Seed the fake from the corpus metadata map.
    fn docs_store(store: HashMap<Uuid, Map<String, Value>>) -> Mutex<HashMap<Uuid, Document>> {
        Mutex::new(
            store
                .into_iter()
                .map(|(id, metadata)| (id, fake_document(id, metadata)))
                .collect(),
        )
    }

    impl DocumentRepo for FakeDocs {
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
                ingested_at: Utc::now(),
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

    struct FakeTexts {
        texts: Mutex<HashMap<Uuid, DocumentText>>,
        fail_versions: bool,
    }

    /// Seed the fake from the corpus parser-version map.
    fn texts_store(versions: HashMap<Uuid, String>) -> Mutex<HashMap<Uuid, DocumentText>> {
        Mutex::new(
            versions
                .into_iter()
                .map(|(id, parser_version)| {
                    (
                        id,
                        DocumentText {
                            document_id: id,
                            text: "canon".to_owned(),
                            normalized_text: "canon".to_owned(),
                            normalization_version: "test".to_owned(),
                            parser: "test".to_owned(),
                            parser_version,
                        },
                    )
                })
                .collect(),
        )
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
            if self.fail_versions {
                return Err(Error::Storage("texts broke".to_owned()));
            }
            let texts = self.texts.lock().expect("fake lock");
            Ok(document_ids
                .iter()
                .filter_map(|id| texts.get(id).map(|row| (*id, row.parser_version.clone())))
                .collect())
        }
        async fn lengths(&self, document_id: Uuid) -> Result<Option<(i64, i64)>> {
            Ok(self
                .texts
                .lock()
                .expect("fake lock")
                .get(&document_id)
                .map(|row| (row.text.len() as i64, row.normalized_text.len() as i64)))
        }
        async fn missing_document_ids(&self, _limit: Option<i64>) -> Result<Vec<Uuid>> {
            // The fake knows no wider universe: nothing is ever missing.
            Ok(Vec::new())
        }
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
            created_at: Utc::now(),
        }
    }

    struct FakePassages {
        regions: Vec<(i64, i64)>,
        stored: Mutex<HashMap<Uuid, Passage>>,
        embeddings: Mutex<HashMap<(Uuid, String, String), Vec<f64>>>,
        fail_covering: bool,
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
                    created_at: Utc::now(),
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

    struct FakeEditions {
        editions: Mutex<HashMap<String, Edition>>,
        fail_list: bool,
        fail_get: bool,
    }

    /// Seed the fake from the corpus key set plus any id-keyed rows.
    fn editions_store(
        keys: HashSet<String>,
        by_id: HashMap<Uuid, String>,
    ) -> Mutex<HashMap<String, Edition>> {
        let mut store = HashMap::new();
        for (id, key) in by_id {
            store.entry(key.clone()).or_insert_with(|| Edition {
                id,
                edition_key: key,
                csl: Map::new(),
                created_at: Utc::now(),
            });
        }
        for key in keys {
            store.entry(key.clone()).or_insert_with(|| Edition {
                id: Uuid::new_v4(),
                edition_key: key,
                csl: Map::new(),
                created_at: Utc::now(),
            });
        }
        Mutex::new(store)
    }

    impl EditionRepo for FakeEditions {
        type Tx = ();
        async fn get(&self, edition_id: Uuid) -> Result<Option<Edition>> {
            if self.fail_get {
                return Err(Error::Storage("editions broke".to_owned()));
            }
            Ok(self
                .editions
                .lock()
                .expect("fake lock")
                .values()
                .find(|edition| edition.id == edition_id)
                .cloned())
        }
        async fn get_by_key(&self, edition_key: &str) -> Result<Option<Edition>> {
            Ok(self
                .editions
                .lock()
                .expect("fake lock")
                .get(edition_key)
                .cloned())
        }
        async fn upsert_key(
            &self,
            _tx: &mut Self::Tx,
            edition_key: &str,
            csl: Option<Map<String, Value>>,
            _lock: bool,
        ) -> Result<Edition> {
            let mut editions = self.editions.lock().expect("fake lock");
            let row = editions
                .entry(edition_key.to_owned())
                .or_insert_with(|| Edition {
                    id: Uuid::new_v4(),
                    edition_key: edition_key.to_owned(),
                    csl: Map::new(),
                    created_at: Utc::now(),
                });
            if let Some(csl) = csl {
                row.csl = csl;
            }
            Ok(row.clone())
        }
        async fn list_keys(&self) -> Result<Vec<String>> {
            if self.fail_list {
                return Err(Error::Storage("editions broke".to_owned()));
            }
            let mut keys: Vec<String> = self
                .editions
                .lock()
                .expect("fake lock")
                .keys()
                .cloned()
                .collect();
            keys.sort();
            Ok(keys)
        }
    }

    struct Corpus {
        docs: HashMap<Uuid, Map<String, Value>>,
        versions: HashMap<Uuid, String>,
        regions: Vec<(i64, i64)>,
        edition_keys: HashSet<String>,
        editions_by_id: HashMap<Uuid, String>,
        fail_docs: bool,
        fail_versions: bool,
        fail_covering: bool,
        fail_edition_list: bool,
        fail_edition_get: bool,
        fail_works: bool,
        fail_revisions: bool,
        fail_blocks: bool,
        fail_waivers: bool,
    }

    impl Default for Corpus {
        fn default() -> Self {
            Self {
                docs: HashMap::from([(doc_id(), edition_metadata("ED1"))]),
                versions: HashMap::from([(doc_id(), "pv2".to_owned())]),
                regions: Vec::new(),
                edition_keys: HashSet::from(["ED1".to_owned()]),
                editions_by_id: HashMap::new(),
                fail_docs: false,
                fail_versions: false,
                fail_covering: false,
                fail_edition_list: false,
                fail_edition_get: false,
                fail_works: false,
                fail_revisions: false,
                fail_blocks: false,
                fail_waivers: false,
            }
        }
    }

    fn make_span(id: Uuid, start: i64, end: i64, parser_version: Option<&str>) -> SourceSpan {
        SourceSpan {
            id,
            document_id: doc_id(),
            char_start: start,
            char_end: end,
            quoted_text: "span text".to_owned(),
            parser: Some("test".to_owned()),
            parser_version: parser_version.map(str::to_owned),
            passage_id: None,
            created_at: Utc::now(),
        }
    }

    fn make_item(
        occurrence_id: Uuid,
        position: i64,
        edition_key: Option<&str>,
        edition_id: Option<Uuid>,
        span_id: Option<Uuid>,
        quoted: Option<&str>,
        verify_status: Option<&str>,
    ) -> CitationItem {
        CitationItem {
            occurrence_id,
            position,
            edition_id,
            edition_key: edition_key.map(str::to_owned),
            source_span_id: span_id,
            quoted_text: quoted.map(str::to_owned),
            verify_status: verify_status.map(str::to_owned),
            verified_at: None,
            locator: Map::new(),
            prefix: None,
            suffix: None,
            suppress_author: false,
        }
    }

    fn make_occurrence(
        id: Uuid,
        citation_key: Uuid,
        block_id: Uuid,
        intent: Intent,
    ) -> CitationOccurrence {
        CitationOccurrence {
            id,
            citation_key,
            block_id,
            placement: Placement::Inline,
            intent,
            note: None,
            created_at: Utc::now(),
        }
    }

    fn make_block(id: Uuid, key: Uuid, block_type: &str, body: &str) -> WorkBlock {
        WorkBlock {
            id,
            revision_id: block_uuid(0x9999_9999_9999_9999_9999_9999_9999_9999),
            block_key: key,
            parent_id: None,
            position: 0,
            block_type: block_type.to_owned(),
            title: None,
            body_markdown: body.to_owned(),
            attributes: Map::new(),
            created_at: Utc::now(),
            updated_at: Utc::now(),
        }
    }

    fn make_work() -> Work {
        Work {
            id: block_uuid(0x3333_3333_3333_3333_3333_3333_3333_3333),
            slug: "essay".to_owned(),
            title: "Essay".to_owned(),
            work_type: "essay".to_owned(),
            status: WorkStatus::Draft,
            language: None,
            abstract_text: None,
            current_revision_id: None,
            metadata: Map::new(),
            created_at: Utc::now(),
            updated_at: Utc::now(),
            archived_at: None,
        }
    }

    fn make_revision(state: RevisionState, content_hash: Option<Vec<u8>>) -> WorkRevision {
        WorkRevision {
            id: block_uuid(0x4444_4444_4444_4444_4444_4444_4444_4444),
            work_id: block_uuid(0x3333_3333_3333_3333_3333_3333_3333_3333),
            revision_number: 1,
            parent_revision_id: None,
            state,
            message: None,
            content_hash,
            created_by: "user".to_owned(),
            created_at: Utc::now(),
            frozen_at: None,
            published_at: None,
            metadata: Map::new(),
        }
    }

    fn make_view(
        blocks: Vec<crate::assembly::AssembledBlock>,
        spans: HashMap<Uuid, SourceSpan>,
        state: RevisionState,
        content_hash: Option<Vec<u8>>,
    ) -> AssembledRevision {
        AssembledRevision {
            work: make_work(),
            revision: make_revision(state, content_hash),
            blocks,
            spans,
        }
    }

    fn assembled(
        block: WorkBlock,
        citations: Vec<BlockCitations>,
        links: Option<BlockLinks>,
    ) -> crate::assembly::AssembledBlock {
        crate::assembly::AssembledBlock {
            block,
            parent_key: None,
            citations,
            links,
        }
    }

    fn run_checker(corpus: Corpus, view: &AssembledRevision) -> Vec<ValidationFinding> {
        let editions = FakeEditions {
            editions: editions_store(corpus.edition_keys, corpus.editions_by_id),
            fail_list: corpus.fail_edition_list,
            fail_get: corpus.fail_edition_get,
        };
        let documents = FakeDocs {
            docs: docs_store(corpus.docs),
            fail: corpus.fail_docs,
        };
        let texts = FakeTexts {
            texts: texts_store(corpus.versions),
            fail_versions: corpus.fail_versions,
        };
        let passages = FakePassages {
            regions: corpus.regions,
            fail_covering: corpus.fail_covering,
            stored: Mutex::new(HashMap::new()),
            embeddings: Mutex::new(HashMap::new()),
        };
        let mut checker = RevisionChecker::new(view, &editions, &documents, &texts, &passages);
        block_on(checker.run()).expect("fake corpus never fails the exercised paths")
    }

    fn rules(findings: &[ValidationFinding]) -> Vec<&str> {
        findings
            .iter()
            .map(|finding| finding.rule_id.as_str())
            .collect()
    }

    fn finding<'a>(findings: &'a [ValidationFinding], rule_id: &str) -> &'a ValidationFinding {
        findings
            .iter()
            .find(|finding| finding.rule_id == rule_id)
            .unwrap_or_else(|| panic!("expected finding {rule_id}"))
    }

    #[test]
    #[should_panic(expected = "expected finding NOPE")]
    fn test_finding_helper_names_the_missing_rule() {
        finding(&[], "NOPE");
    }
    fn severity<'a>(findings: &'a [ValidationFinding], rule_id: &str) -> &'a str {
        finding(findings, rule_id).severity.as_str()
    }

    fn check_finding(
        rule_id: &str,
        severity: &str,
        block_key: Option<&str>,
        citation_key: Option<&str>,
    ) -> ValidationFinding {
        ValidationFinding {
            rule_id: rule_id.to_owned(),
            severity: severity.to_owned(),
            block_key: block_key.map(str::to_owned),
            citation_key: citation_key.map(str::to_owned),
            message: "m".to_owned(),
            detail: None,
        }
    }

    #[test]
    fn gate_parses_and_displays() {
        assert_eq!(
            ValidateGate::parse("none").expect("none"),
            ValidateGate::None
        );
        assert_eq!(
            ValidateGate::parse("freeze").expect("freeze"),
            ValidateGate::Freeze
        );
        assert_eq!(
            ValidateGate::parse("publish").expect("publish"),
            ValidateGate::Publish
        );
        assert_eq!(ValidateGate::None.as_str(), "none");
        assert_eq!(ValidateGate::Freeze.as_str(), "freeze");
        assert_eq!(ValidateGate::Publish.as_str(), "publish");
        assert_eq!(ValidateGate::Freeze.to_string(), "freeze");
    }

    #[test]
    fn gate_parse_rejects_unknown_with_repr() {
        let error = ValidateGate::parse("review").expect_err("unknown gate");
        assert_eq!(
            error.to_string(),
            "data validation failed: Unknown gate 'review'"
        );
    }

    #[test]
    fn intent_and_grounded_predicates() {
        assert!(is_narrow_intent("quotation"));
        assert!(is_narrow_intent("translation"));
        assert!(!is_narrow_intent("support"));
        assert!(is_region_warn_intent("support"));
        assert!(is_region_warn_intent("source"));
        assert!(is_region_warn_intent("definition"));
        assert!(!is_region_warn_intent("quotation"));
        assert!(!is_region_warn_intent("background"));
        assert!(is_grounded_type("translation_unit"));
        assert!(is_grounded_type("quotation"));
        assert!(!is_grounded_type("paragraph"));
    }

    #[test]
    fn policy_floor_holds_without_policy() {
        let policy = HashMap::new();
        assert_eq!(
            resolve_severity(&policy, "essay", "AUTH_SPAN_REGION", "warning"),
            "warning"
        );
    }

    #[test]
    fn policy_pack_can_escalate_and_allow() {
        let policy = HashMap::from([(
            "translation".to_owned(),
            HashMap::from([
                ("AUTH_SPAN_REGION".to_owned(), "error".to_owned()),
                ("AUTH_FILE_DRIFT".to_owned(), "allow".to_owned()),
            ]),
        )]);
        assert_eq!(
            resolve_severity(&policy, "translation", "AUTH_SPAN_REGION", "warning"),
            "error"
        );
        assert_eq!(
            resolve_severity(&policy, "translation", "AUTH_FILE_DRIFT", "warning"),
            "allow"
        );
    }

    #[test]
    fn policy_warn_spelling_and_other_types_fall_back() {
        let policy = HashMap::from([(
            "essay".to_owned(),
            HashMap::from([
                ("AUTH_SPAN_REGION".to_owned(), "warn".to_owned()),
                ("AUTH_FILE_DRIFT".to_owned(), "sometimes".to_owned()),
            ]),
        )]);
        assert_eq!(
            resolve_severity(&policy, "essay", "AUTH_SPAN_REGION", "warning"),
            "warning"
        );
        assert_eq!(
            resolve_severity(&policy, "dossier", "AUTH_SPAN_REGION", "warning"),
            "warning"
        );
        // An unknown value is a typo, not a silencer: the floor holds.
        assert_eq!(
            resolve_severity(&policy, "essay", "AUTH_FILE_DRIFT", "warning"),
            "warning"
        );
    }

    #[test]
    fn blockers_none_lists_but_never_blocks() {
        let findings = vec![check_finding(
            "AUTH_QUOTE_UNVERIFIED",
            "error",
            Some("b"),
            Some("c"),
        )];
        assert_eq!(
            validation_blockers(&findings, ValidateGate::None, &HashSet::new()),
            Vec::<String>::new()
        );
    }

    #[test]
    fn blockers_freeze_errors_only_sorted() {
        let findings = vec![
            check_finding("AUTH_QUOTE_UNVERIFIED", "error", Some("b"), Some("c")),
            check_finding("AUTH_SPAN_REGION", "warning", Some("b"), Some("c")),
            check_finding("AUTH_DOCUMENT_UNKNOWN", "error", Some("b"), Some("c")),
            check_finding("AUTH_DOCUMENT_UNKNOWN", "error", Some("b2"), Some("c2")),
        ];
        assert_eq!(
            validation_blockers(&findings, ValidateGate::Freeze, &HashSet::new()),
            vec![
                "AUTH_DOCUMENT_UNKNOWN".to_owned(),
                "AUTH_QUOTE_UNVERIFIED".to_owned()
            ]
        );
    }

    #[test]
    fn blockers_edition_missing_only_at_publish() {
        let findings = vec![check_finding(
            "AUTH_CITATION_EDITION_MISSING",
            "warning",
            Some("b"),
            Some("c"),
        )];
        assert_eq!(
            validation_blockers(&findings, ValidateGate::Freeze, &HashSet::new()),
            Vec::<String>::new()
        );
        assert_eq!(
            validation_blockers(&findings, ValidateGate::Publish, &HashSet::new()),
            vec!["AUTH_CITATION_EDITION_MISSING".to_owned()]
        );
    }

    #[test]
    fn blockers_allow_never_blocks() {
        let findings = vec![
            check_finding(
                "AUTH_CITATION_EDITION_MISSING",
                "allow",
                Some("b"),
                Some("c"),
            ),
            check_finding("AUTH_QUOTE_UNVERIFIED", "allow", Some("b"), Some("c")),
        ];
        assert_eq!(
            validation_blockers(&findings, ValidateGate::Publish, &HashSet::new()),
            Vec::<String>::new()
        );
    }

    #[test]
    fn blockers_waived_subject_and_global() {
        let findings = vec![
            check_finding("AUTH_QUOTE_UNVERIFIED", "error", Some("b"), Some("c1")),
            check_finding("AUTH_SPAN_NOT_NARROWED", "error", Some("b"), Some("c2")),
        ];
        // The exact (rule, subject) pair clears.
        let waived = HashSet::from([("AUTH_QUOTE_UNVERIFIED".to_owned(), Some("c1".to_owned()))]);
        assert_eq!(
            validation_blockers(&findings, ValidateGate::Freeze, &waived),
            vec!["AUTH_SPAN_NOT_NARROWED".to_owned()]
        );
        // A (rule, None) waiver clears globally, across subjects.
        let global = HashSet::from([
            ("AUTH_QUOTE_UNVERIFIED".to_owned(), None),
            ("AUTH_SPAN_NOT_NARROWED".to_owned(), None),
        ]);
        assert_eq!(
            validation_blockers(&findings, ValidateGate::Freeze, &global),
            Vec::<String>::new()
        );
        // A waiver for another subject clears nothing.
        let other = HashSet::from([("AUTH_QUOTE_UNVERIFIED".to_owned(), Some("c9".to_owned()))]);
        assert_eq!(
            validation_blockers(&findings, ValidateGate::Freeze, &other),
            vec![
                "AUTH_QUOTE_UNVERIFIED".to_owned(),
                "AUTH_SPAN_NOT_NARROWED".to_owned()
            ]
        );
    }

    #[test]
    fn citation_checks_group_first_span_and_tier() {
        let cite_key = block_uuid(0xaaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa);
        let block_id = block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb);
        let span_id = block_uuid(0xcccc_cccc_cccc_cccc_cccc_cccc_cccc_cccc);
        let missing_id = block_uuid(0xdddd_dddd_dddd_dddd_dddd_dddd_dddd_dddd);
        let occurrence_id = block_uuid(0xeeee_eeee_eeee_eeee_eeee_eeee_eeee_eeee);
        let occurrence = make_occurrence(occurrence_id, cite_key, block_id, Intent::Quotation);
        let items = vec![
            make_item(
                occurrence_id,
                1,
                Some("ED1"),
                None,
                Some(missing_id),
                Some("q"),
                None,
            ),
            make_item(
                occurrence_id,
                0,
                Some("ED1"),
                None,
                Some(span_id),
                Some("q"),
                Some("exact"),
            ),
        ];
        let block = make_block(
            block_id,
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            "words",
        );
        let view = make_view(
            vec![assembled(
                block.clone(),
                vec![BlockCitations { occurrence, items }],
                None,
            )],
            HashMap::from([(span_id, make_span(span_id, 10, 60, Some("pv2")))]),
            RevisionState::Draft,
            None,
        );
        let findings = vec![
            check_finding(
                "AUTH_QUOTE_UNVERIFIED",
                "error",
                Some("b"),
                Some(&cite_key.to_string()),
            ),
            check_finding("AUTH_SPAN_REGION", "warning", Some("b"), Some("other")),
        ];
        let checks = citation_checks(&view, &findings);
        assert_eq!(checks.len(), 1);
        assert_eq!(checks[0].citation_key, cite_key.to_string());
        assert_eq!(checks[0].block_key, block.block_key.to_string());
        assert_eq!(checks[0].intent, "quotation");
        // First span in view order skips the missing row, landing on (10, 60).
        assert_eq!(checks[0].char_start, Some(10));
        assert_eq!(checks[0].char_end, Some(60));
        // First truthy verify status skips the empty row.
        assert_eq!(checks[0].tier, Some("exact".to_owned()));
        assert_eq!(checks[0].findings, vec!["AUTH_QUOTE_UNVERIFIED"]);
    }

    #[test]
    fn citation_checks_empty_items_have_no_span_or_tier() {
        let cite_key = block_uuid(0xaaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa);
        let block_id = block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb);
        let occurrence_id = block_uuid(0xeeee_eeee_eeee_eeee_eeee_eeee_eeee_eeee);
        let occurrence = make_occurrence(occurrence_id, cite_key, block_id, Intent::Support);
        let block = make_block(
            block_id,
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            "words",
        );
        let view = make_view(
            vec![assembled(
                block,
                vec![BlockCitations {
                    occurrence,
                    items: vec![],
                }],
                None,
            )],
            HashMap::new(),
            RevisionState::Draft,
            None,
        );
        let checks = citation_checks(&view, &[]);
        assert_eq!(checks.len(), 1);
        assert_eq!(checks[0].tier, None);
        assert_eq!(checks[0].char_start, None);
        assert_eq!(checks[0].char_end, None);
        assert_eq!(checks[0].findings, Vec::<String>::new());
    }

    /// One clean occurrence: paragraph block, marker present, edition keyed,
    /// span versioned, quote exact, span narrowed.
    fn clean_fixture() -> (Corpus, AssembledRevision, Uuid, Uuid) {
        let cite_key = block_uuid(0xaaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa);
        let block_id = block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb);
        let block_key = block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff);
        let occurrence_id = block_uuid(0xeeee_eeee_eeee_eeee_eeee_eeee_eeee_eeee);
        let span_id = block_uuid(0xcccc_cccc_cccc_cccc_cccc_cccc_cccc_cccc);
        let occurrence = make_occurrence(occurrence_id, cite_key, block_id, Intent::Quotation);
        let item = make_item(
            occurrence_id,
            0,
            Some("ED1"),
            None,
            Some(span_id),
            Some("a fine sentence here"),
            Some("exact"),
        );
        let body = crate::markers::format_marker(&cite_key);
        let block = make_block(block_id, block_key, "paragraph", &body);
        let view = make_view(
            vec![assembled(
                block,
                vec![BlockCitations {
                    occurrence,
                    items: vec![item],
                }],
                None,
            )],
            HashMap::from([(span_id, make_span(span_id, 10, 60, Some("pv2")))]),
            RevisionState::Draft,
            None,
        );
        (Corpus::default(), view, cite_key, span_id)
    }

    #[test]
    fn clean_occurrence_has_no_findings() {
        let (corpus, view, _, _) = clean_fixture();
        assert_eq!(run_checker(corpus, &view), vec![]);
    }

    #[test]
    fn invalid_marker_names_no_citation() {
        let block = make_block(
            block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb),
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            "see {{cite:c1}} here",
        );
        let view = make_view(
            vec![assembled(block, vec![], None)],
            HashMap::new(),
            RevisionState::Draft,
            None,
        );
        let findings = run_checker(Corpus::default(), &view);
        assert_eq!(rules(&findings), vec!["AUTH_CITATION_MARKER_DANGLING"]);
        let dangling = finding(&findings, "AUTH_CITATION_MARKER_DANGLING");
        assert_eq!(dangling.severity, "error");
        assert_eq!(
            dangling.message,
            "Marker {{cite:c1}} names no citation: keys are UUIDs"
        );
        assert_eq!(
            dangling
                .detail
                .as_ref()
                .and_then(|detail| detail.get("marker")),
            Some(&Value::String("{{cite:c1}}".to_owned()))
        );
    }

    #[test]
    fn occurrence_without_marker_is_missing() {
        let (corpus, mut view, cite_key, _) = clean_fixture();
        view.blocks[0].block.body_markdown = "no markers here".to_owned();
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_CITATION_MARKER_MISSING"]);
        assert_eq!(
            finding(&findings, "AUTH_CITATION_MARKER_MISSING").message,
            format!("Occurrence {cite_key} has no {{{{cite:…}}}} marker in its block")
        );
    }

    #[test]
    fn valid_marker_without_occurrence_dangles() {
        let cite_key = block_uuid(0xaaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa);
        let block = make_block(
            block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb),
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            &crate::markers::format_marker(&cite_key),
        );
        let view = make_view(
            vec![assembled(block, vec![], None)],
            HashMap::new(),
            RevisionState::Draft,
            None,
        );
        let findings = run_checker(Corpus::default(), &view);
        assert_eq!(rules(&findings), vec!["AUTH_CITATION_MARKER_DANGLING"]);
        assert_eq!(
            finding(&findings, "AUTH_CITATION_MARKER_DANGLING").message,
            format!("Marker {{{{cite:{cite_key}}}}} matches no occurrence on this block")
        );
        assert_eq!(
            finding(&findings, "AUTH_CITATION_MARKER_DANGLING")
                .detail
                .as_ref()
                .and_then(|detail| detail.get("citation_key")),
            Some(&Value::String(cite_key.to_string()))
        );
    }

    #[test]
    fn marker_on_another_block_is_unused() {
        let (corpus, mut view, cite_key, _) = clean_fixture();
        let home_key = view.blocks[0].block.block_key;
        let other = make_block(
            block_uuid(0x1111_1111_1111_1111_1111_1111_1111_1111),
            block_uuid(0x2222_2222_2222_2222_2222_2222_2222_2222),
            "paragraph",
            &crate::markers::format_marker(&cite_key),
        );
        view.blocks[0].block.body_markdown = "moved away".to_owned();
        view.blocks.push(assembled(other, vec![], None));
        let findings = run_checker(corpus, &view);
        assert!(rules(&findings).contains(&"AUTH_CITATION_MARKER_MISSING"));
        let unused = finding(&findings, "AUTH_UNUSED_CITATION");
        assert_eq!(unused.severity, "warning");
        assert_eq!(unused.block_key, Some(home_key.to_string()));
        assert_eq!(unused.citation_key, Some(cite_key.to_string()));
        assert_eq!(
            unused.message,
            format!(
                "Occurrence {cite_key} is marked in another block, not the one rendered with it"
            )
        );
    }

    #[test]
    fn edition_missing_is_a_warning() {
        let (corpus, mut view, _, span_id) = clean_fixture();
        let occurrence_id = view.blocks[0].citations[0].occurrence.id;
        view.blocks[0].citations[0].items = vec![make_item(
            occurrence_id,
            0,
            None,
            None,
            Some(span_id),
            Some("a fine sentence here"),
            Some("exact"),
        )];
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_CITATION_EDITION_MISSING"]);
        assert_eq!(
            severity(&findings, "AUTH_CITATION_EDITION_MISSING"),
            "warning"
        );
        assert_eq!(
            finding(&findings, "AUTH_CITATION_EDITION_MISSING").message,
            "The citation names no edition: edition_key or edition_id"
        );
    }

    #[test]
    fn unknown_edition_key_warns_and_mismatches() {
        // No editions row knows ED1, and the document disagrees with it too.
        let (mut corpus, mut view, _, span_id) = clean_fixture();
        corpus.edition_keys = HashSet::new();
        corpus.docs = HashMap::from([(doc_id(), edition_metadata("OTHER"))]);
        let occurrence_id = view.blocks[0].citations[0].occurrence.id;
        view.blocks[0].citations[0].items = vec![make_item(
            occurrence_id,
            0,
            Some("ED1"),
            None,
            Some(span_id),
            Some("a fine sentence here"),
            Some("exact"),
        )];
        let findings = run_checker(corpus, &view);
        assert_eq!(
            rules(&findings),
            vec!["AUTH_EDITION_KEY_UNKNOWN", "AUTH_CITATION_EDITION_MISMATCH"]
        );
        assert_eq!(
            finding(&findings, "AUTH_EDITION_KEY_UNKNOWN").message,
            "edition_key ED1 has no bibliography.editions row"
        );
    }

    #[test]
    fn spanless_item_is_bibliography_only() {
        let (corpus, mut view, _, _) = clean_fixture();
        let occurrence_id = view.blocks[0].citations[0].occurrence.id;
        view.blocks[0].citations[0].items = vec![make_item(
            occurrence_id,
            0,
            Some("ED1"),
            None,
            None,
            Some("a fine sentence here"),
            Some("exact"),
        )];
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_BIBLIOGRAPHY_ONLY"]);
        assert_eq!(
            finding(&findings, "AUTH_BIBLIOGRAPHY_ONLY").message,
            "The citation has identity but no span: bibliography, not evidence"
        );
    }

    #[test]
    fn span_absent_from_view_is_silent() {
        // Assembly only indexes spans the span repo returns, so a missing
        // row is unreachable, not stale: silence is exact.
        let (corpus, mut view, _, _) = clean_fixture();
        let occurrence_id = view.blocks[0].citations[0].occurrence.id;
        let missing = block_uuid(0xdddd_dddd_dddd_dddd_dddd_dddd_dddd_dddd);
        view.blocks[0].citations[0].items = vec![make_item(
            occurrence_id,
            0,
            Some("ED1"),
            None,
            Some(missing),
            Some("a fine sentence here"),
            Some("exact"),
        )];
        assert_eq!(run_checker(corpus, &view), vec![]);
    }

    #[test]
    fn unknown_document_stops_the_item() {
        let (mut corpus, view, _, _) = clean_fixture();
        corpus.docs = HashMap::new();
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_DOCUMENT_UNKNOWN"]);
        assert_eq!(
            finding(&findings, "AUTH_DOCUMENT_UNKNOWN").message,
            format!("Document {} does not exist", doc_id())
        );
    }

    #[test]
    fn document_without_text_is_uncheckable() {
        let (mut corpus, view, _, _) = clean_fixture();
        corpus.versions = HashMap::new();
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_SOURCE_UNCHECKABLE"]);
        assert_eq!(
            finding(&findings, "AUTH_SOURCE_UNCHECKABLE").message,
            format!("Document {} has no canonical text", doc_id())
        );
    }

    #[test]
    fn reparse_stales_the_span_but_continues() {
        let (corpus, mut view, _, span_id) = clean_fixture();
        view.spans.get_mut(&span_id).expect("span").parser_version = Some("pv1".to_owned());
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_SOURCE_SPAN_STALE"]);
        let stale = finding(&findings, "AUTH_SOURCE_SPAN_STALE");
        assert_eq!(
            stale.message,
            "The document was re-parsed since this span resolved: re-verify it"
        );
        assert_eq!(
            stale
                .detail
                .as_ref()
                .and_then(|detail| detail.get("span_parser_version")),
            Some(&Value::String("pv1".to_owned()))
        );
        assert_eq!(
            stale
                .detail
                .as_ref()
                .and_then(|detail| detail.get("document_parser_version")),
            Some(&Value::String("pv2".to_owned()))
        );
    }

    #[test]
    fn document_without_key_is_unknown() {
        let (mut corpus, view, _, _) = clean_fixture();
        corpus.docs = HashMap::from([(doc_id(), Map::new())]);
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_EDITION_KEY_UNKNOWN"]);
        assert_eq!(
            finding(&findings, "AUTH_EDITION_KEY_UNKNOWN").message,
            "edition_key ED1 is on no ingested document yet"
        );
    }

    #[test]
    fn differing_key_is_a_mismatch() {
        let (mut corpus, view, _, _) = clean_fixture();
        corpus.docs = HashMap::from([(doc_id(), edition_metadata("OTHER"))]);
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_CITATION_EDITION_MISMATCH"]);
        let mismatch = finding(&findings, "AUTH_CITATION_EDITION_MISMATCH");
        assert_eq!(
            mismatch.message,
            "Citation key ED1 differs from the span's document key OTHER"
        );
        assert_eq!(
            mismatch
                .detail
                .as_ref()
                .and_then(|detail| detail.get("item_key")),
            Some(&Value::String("ED1".to_owned()))
        );
        assert_eq!(
            mismatch
                .detail
                .as_ref()
                .and_then(|detail| detail.get("document_key")),
            Some(&Value::String("OTHER".to_owned()))
        );
    }

    #[test]
    fn edition_id_resolves_its_key() {
        let edition_id = block_uuid(0xeeee_0000_0000_0000_0000_0000_0000_0000);
        let (mut corpus, mut view, _, span_id) = clean_fixture();
        corpus.editions_by_id = HashMap::from([(edition_id, "ED1".to_owned())]);
        let occurrence_id = view.blocks[0].citations[0].occurrence.id;
        view.blocks[0].citations[0].items = vec![make_item(
            occurrence_id,
            0,
            None,
            Some(edition_id),
            Some(span_id),
            Some("a fine sentence here"),
            Some("exact"),
        )];
        assert_eq!(run_checker(corpus, &view), vec![]);
    }

    #[test]
    fn unverified_quote_names_its_status() {
        for (status, message_status) in [
            (Some("near"), "near"),
            (Some("stale"), "stale"),
            (None, "None"),
        ] {
            let (corpus, mut view, _, span_id) = clean_fixture();
            let occurrence_id = view.blocks[0].citations[0].occurrence.id;
            view.blocks[0].citations[0].items = vec![make_item(
                occurrence_id,
                0,
                Some("ED1"),
                None,
                Some(span_id),
                Some("a fine sentence here"),
                status,
            )];
            let findings = run_checker(corpus, &view);
            assert_eq!(rules(&findings), vec!["AUTH_QUOTE_UNVERIFIED"]);
            let unverified = finding(&findings, "AUTH_QUOTE_UNVERIFIED");
            assert_eq!(
                unverified.message,
                format!(
                    "Quote verifies {message_status}, not exact or normalized: earn a waiver or re-anchor it"
                )
            );
            assert_eq!(
                unverified
                    .detail
                    .as_ref()
                    .and_then(|detail| detail.get("verify_status")),
                Some(&status.map(str::to_owned).map_or(Value::Null, Value::String))
            );
        }
    }

    #[test]
    fn quotation_on_a_region_is_not_narrowed() {
        let (mut corpus, view, _, _) = clean_fixture();
        corpus.regions = vec![(10, 60)];
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_SPAN_NOT_NARROWED"]);
        assert_eq!(
            finding(&findings, "AUTH_SPAN_NOT_NARROWED").message,
            "A quotation or translation must cite a narrowed span, not a whole passage"
        );
    }

    #[test]
    fn long_quotation_is_not_narrowed_without_any_region() {
        let (corpus, mut view, _, span_id) = clean_fixture();
        let span = view.spans.get_mut(&span_id).expect("span");
        span.char_start = 0;
        span.char_end = MAX_QUOTE_CHARS + 1;
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_SPAN_NOT_NARROWED"]);
    }

    #[test]
    fn support_on_a_region_is_a_warning() {
        let (mut corpus, mut view, _, _) = clean_fixture();
        corpus.regions = vec![(10, 60)];
        view.blocks[0].citations[0].occurrence.intent = Intent::Support;
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_SPAN_REGION"]);
        assert_eq!(severity(&findings, "AUTH_SPAN_REGION"), "warning");
    }

    #[test]
    fn ungrounded_quotation_block_leans_on_nothing() {
        let block = make_block(
            block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb),
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "quotation",
            "bare words",
        );
        let view = make_view(
            vec![assembled(block, vec![], None)],
            HashMap::new(),
            RevisionState::Draft,
            None,
        );
        let findings = run_checker(Corpus::default(), &view);
        assert_eq!(rules(&findings), vec!["AUTH_BLOCK_UNGROUNDED"]);
        assert_eq!(
            finding(&findings, "AUTH_BLOCK_UNGROUNDED").message,
            "A quotation block with no citation or source link leans on nothing"
        );
    }

    #[test]
    fn source_link_grounds_the_block() {
        let block_id = block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb);
        let block = make_block(
            block_id,
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "quotation",
            "bare words",
        );
        let links = BlockLinks {
            sources: vec![BlockSourceLink {
                block_id,
                source_span_id: block_uuid(0xcccc_cccc_cccc_cccc_cccc_cccc_cccc_cccc),
                relation: "cites".to_owned(),
                confidence: None,
                note: None,
                created_at: Utc::now(),
            }],
            entities: Vec::new(),
        };
        let view = make_view(
            vec![assembled(block, vec![], Some(links))],
            HashMap::new(),
            RevisionState::Draft,
            None,
        );
        assert_eq!(run_checker(Corpus::default(), &view), vec![]);
    }

    #[test]
    fn parent_in_another_revision_mismatches() {
        let mut block = make_block(
            block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb),
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            "words",
        );
        block.parent_id = Some(block_uuid(0x1234_1234_1234_1234_1234_1234_5678_9012));
        let view = make_view(
            vec![assembled(block, vec![], None)],
            HashMap::new(),
            RevisionState::Draft,
            None,
        );
        let findings = run_checker(Corpus::default(), &view);
        assert_eq!(rules(&findings), vec!["AUTH_PARENT_REVISION_MISMATCH"]);
        assert_eq!(
            finding(&findings, "AUTH_PARENT_REVISION_MISMATCH").message,
            "The block's parent is in another revision"
        );
    }

    #[test]
    fn parent_present_in_revision_passes() {
        // The mismatch fires only when no view block carries the parent id;
        // a parent rendered in the same revision is silent.
        let parent_id = block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb);
        let parent = make_block(
            parent_id,
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            "words",
        );
        let mut child = make_block(
            block_uuid(0xcccc_cccc_cccc_cccc_cccc_cccc_cccc_cccc),
            block_uuid(0xdddd_dddd_dddd_dddd_dddd_dddd_dddd_dddd),
            "paragraph",
            "words",
        );
        child.parent_id = Some(parent_id);
        let view = make_view(
            vec![
                assembled(parent, vec![], None),
                assembled(child, vec![], None),
            ],
            HashMap::new(),
            RevisionState::Draft,
            None,
        );
        assert_eq!(run_checker(Corpus::default(), &view), vec![]);
    }

    #[test]
    fn license_quota_boundaries() {
        let cite_key = block_uuid(0xaaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa);
        let block_id = block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb);
        let occurrence_id = block_uuid(0xeeee_eeee_eeee_eeee_eeee_eeee_eeee_eeee);
        let span_id = block_uuid(0xcccc_cccc_cccc_cccc_cccc_cccc_cccc_cccc);
        let build = |first: String, second: &str| {
            let occurrence = make_occurrence(occurrence_id, cite_key, block_id, Intent::Quotation);
            // Paragraph blocks carry no grounding requirement; the marker is
            // present so only the quota can fire.
            let block = make_block(
                block_id,
                block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
                "paragraph",
                &crate::markers::format_marker(&cite_key),
            );
            make_view(
                vec![assembled(
                    block,
                    vec![BlockCitations {
                        occurrence,
                        items: vec![
                            make_item(
                                occurrence_id,
                                0,
                                Some("ED1"),
                                None,
                                Some(span_id),
                                Some(&first),
                                Some("exact"),
                            ),
                            make_item(
                                occurrence_id,
                                1,
                                Some("ED1"),
                                None,
                                Some(span_id),
                                Some(second),
                                Some("exact"),
                            ),
                        ],
                    }],
                    None,
                )],
                HashMap::from([(span_id, make_span(span_id, 10, 60, Some("pv2")))]),
                RevisionState::Draft,
                None,
            )
        };
        // 999 + 1 == 1000: at the cap, no finding.
        let view = build("x".repeat(999), "y");
        assert_eq!(run_checker(Corpus::default(), &view), vec![]);
        // 1000 + 1 == 1001: above the cap, one finding with sorted keys.
        let view = build("x".repeat(1000), "y");
        let findings = run_checker(Corpus::default(), &view);
        assert_eq!(rules(&findings), vec!["AUTH_LICENSE_EXPORT"]);
        let quota = finding(&findings, "AUTH_LICENSE_EXPORT");
        assert_eq!(
            quota.message,
            format!(
                "Stored quotations copy 1001 characters from document {}, above the 1000-character cap",
                doc_id()
            )
        );
        assert_eq!(
            quota
                .detail
                .as_ref()
                .and_then(|detail| detail.get("quoted_characters")),
            Some(&Value::from(1001i64))
        );
        assert_eq!(
            quota.detail.as_ref().and_then(|detail| detail.get("cap")),
            Some(&Value::from(1000i64))
        );
        assert_eq!(
            quota
                .detail
                .as_ref()
                .and_then(|detail| detail.get("citation_keys")),
            Some(&Value::Array(vec![Value::String(cite_key.to_string())]))
        );
        // Multibyte text counts characters, not bytes: 1001 e-acutes quote
        // 1001 characters while occupying 2002 bytes.
        let view = build("é".repeat(1001), "");
        let findings = run_checker(Corpus::default(), &view);
        assert_eq!(rules(&findings), vec!["AUTH_LICENSE_EXPORT"]);
        assert_eq!(
            finding(&findings, "AUTH_LICENSE_EXPORT")
                .detail
                .as_ref()
                .and_then(|detail| detail.get("quoted_characters")),
            Some(&Value::from(1001i64))
        );
    }

    #[test]
    fn test_license_quota_sorts_two_breaching_documents() {
        // Two documents over the cap force the totals sort to compare:
        // the comparator only runs with two or more entries.
        let second_doc = block_uuid(0x0101_0101_0101_0101_0101_0101_0101_0101);
        let cite_key = block_uuid(0xaaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa);
        let block_id = block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb);
        let occurrence_id = block_uuid(0xeeee_eeee_eeee_eeee_eeee_eeee_eeee_eeee);
        let span_id = block_uuid(0xcccc_cccc_cccc_cccc_cccc_cccc_cccc_cccc);
        let span2_id = block_uuid(0xdddd_dddd_dddd_dddd_dddd_dddd_dddd_dddd);
        let occurrence = make_occurrence(occurrence_id, cite_key, block_id, Intent::Quotation);
        let block = make_block(
            block_id,
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            &crate::markers::format_marker(&cite_key),
        );
        // Small char windows keep the narrowed-span check quiet; the long
        // quotes trip only the quota, once per document.
        let span2 = SourceSpan {
            id: span2_id,
            document_id: second_doc,
            char_start: 0,
            char_end: 5,
            quoted_text: "span text".to_owned(),
            parser: Some("test".to_owned()),
            parser_version: Some("pv2".to_owned()),
            passage_id: None,
            created_at: Utc::now(),
        };
        let view = make_view(
            vec![assembled(
                block,
                vec![BlockCitations {
                    occurrence,
                    items: vec![
                        make_item(
                            occurrence_id,
                            0,
                            Some("ED1"),
                            None,
                            Some(span_id),
                            Some(&"x".repeat(1001)),
                            Some("exact"),
                        ),
                        make_item(
                            occurrence_id,
                            1,
                            Some("ED1"),
                            None,
                            Some(span2_id),
                            Some(&"y".repeat(1001)),
                            Some("exact"),
                        ),
                    ],
                }],
                None,
            )],
            HashMap::from([
                (span_id, make_span(span_id, 10, 60, Some("pv2"))),
                (span2_id, span2),
            ]),
            RevisionState::Draft,
            None,
        );
        let mut corpus = Corpus::default();
        corpus.docs.insert(second_doc, edition_metadata("ED1"));
        corpus.versions.insert(second_doc, "pv2".to_owned());
        let findings = run_checker(corpus, &view);
        assert_eq!(
            rules(&findings),
            vec!["AUTH_LICENSE_EXPORT", "AUTH_LICENSE_EXPORT"]
        );
        let mut documents: Vec<String> = findings
            .iter()
            .map(|found| {
                found
                    .detail
                    .as_ref()
                    .and_then(|detail| detail.get("document_id"))
                    .and_then(Value::as_str)
                    .expect("quota names its document")
                    .to_owned()
            })
            .collect();
        documents.sort();
        let mut expected = vec![doc_id().to_string(), second_doc.to_string()];
        expected.sort();
        assert_eq!(documents, expected);
    }

    #[test]
    fn license_quota_dedupes_repeated_items() {
        // The same (occurrence_id, position) row rendered twice — e.g. a
        // copy-forward artifact — quotes once, not twice: 600 stays under
        // the cap where 1200 would breach it.
        let (corpus, mut view, _, span_id) = clean_fixture();
        let occurrence_id = view.blocks[0].citations[0].occurrence.id;
        let item = make_item(
            occurrence_id,
            0,
            Some("ED1"),
            None,
            Some(span_id),
            Some(&"x".repeat(600)),
            Some("exact"),
        );
        view.blocks[0].citations[0].items = vec![item.clone(), item];
        assert_eq!(run_checker(corpus, &view), vec![]);
    }

    #[test]
    fn license_quota_skips_unquoted_and_spanless() {
        let (corpus, mut view, _, span_id) = clean_fixture();
        let occurrence_id = view.blocks[0].citations[0].occurrence.id;
        view.blocks[0].citations[0].items = vec![
            make_item(
                occurrence_id,
                0,
                Some("ED1"),
                None,
                Some(span_id),
                None,
                Some("exact"),
            ),
            make_item(
                occurrence_id,
                1,
                Some("ED1"),
                None,
                None,
                Some(&"x".repeat(2000)),
                Some("exact"),
            ),
        ];
        // The unquoted row has no text and the spanless row has no address;
        // the long spanless quote is bibliography, not a stored copy.
        let findings = run_checker(corpus, &view);
        assert_eq!(rules(&findings), vec!["AUTH_BIBLIOGRAPHY_ONLY"]);
    }

    #[test]
    fn revision_mutation_fixtures() {
        let (corpus, view, _, _) = clean_fixture();
        // Drafts never carry a hash: nothing to compare.
        assert_eq!(run_checker(corpus, &view), vec![]);
        let (corpus, view, _, _) = clean_fixture();
        let mut frozen = view;
        frozen.revision.state = RevisionState::Frozen;
        // A frozen revision without a stored hash is not yet sealed: skip.
        assert_eq!(run_checker(corpus, &frozen), vec![]);
        // The sealed hash matches: clean.
        let (corpus, view, cite_key, _) = clean_fixture();
        let mut sealed = view;
        sealed.revision.state = RevisionState::Frozen;
        sealed.revision.content_hash = Some(hash_assembled(&sealed).to_vec());
        assert_eq!(run_checker(corpus, &sealed), vec![]);
        // One word moved — marker kept, so only the hash can fire.
        sealed.blocks[0].block.body_markdown =
            format!("moved words {}", crate::markers::format_marker(&cite_key));
        let findings = run_checker(Corpus::default(), &sealed);
        assert_eq!(rules(&findings), vec!["AUTH_REVISION_MUTATED"]);
        assert_eq!(
            finding(&findings, "AUTH_REVISION_MUTATED").message,
            "Revision 1 no longer matches its frozen hash"
        );
    }

    struct FakeExporter {
        rendered: Mutex<String>,
        fail: bool,
    }

    impl DraftExporter for FakeExporter {
        async fn export_markdown(&self, _work_id: Uuid) -> Result<String> {
            if self.fail {
                return Err(Error::Storage("export broke".to_owned()));
            }
            Ok(self.rendered.lock().unwrap().clone())
        }
    }

    fn drift_view(rel: Option<&str>) -> (Work, AssembledRevision) {
        let mut revision = make_revision(RevisionState::Draft, None);
        if let Some(rel) = rel {
            let mut port = Map::new();
            port.insert("file".to_owned(), Value::String(rel.to_owned()));
            let mut metadata = Map::new();
            metadata.insert("port".to_owned(), Value::Object(port));
            revision.metadata = metadata;
        }
        let work = make_work();
        let view = AssembledRevision {
            work: work.clone(),
            revision,
            blocks: Vec::new(),
            spans: HashMap::new(),
        };
        (work, view)
    }

    #[test]
    fn drift_needs_a_port_file() {
        let dir = tempfile::tempdir().expect("tempdir");
        let exporter = FakeExporter {
            rendered: Mutex::new("rendered".to_owned()),
            fail: false,
        };
        let (work, view) = drift_view(None);
        let drift = block_on(check_drift_file(dir.path(), &exporter, &work, &view))
            .expect("drift check reads no repos");
        assert_eq!(drift, None);
    }

    #[test]
    fn drift_needs_an_existing_file() {
        let dir = tempfile::tempdir().expect("tempdir");
        let exporter = FakeExporter {
            rendered: Mutex::new("rendered".to_owned()),
            fail: false,
        };
        let (work, view) = drift_view(Some("essay.md"));
        let drift = block_on(check_drift_file(dir.path(), &exporter, &work, &view))
            .expect("drift check reads no repos");
        assert_eq!(drift, None);
    }

    #[test]
    fn drift_fires_on_changed_files() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("essay.md"), "moved words").expect("fixture file");
        let exporter = FakeExporter {
            rendered: Mutex::new("rendered".to_owned()),
            fail: false,
        };
        let (work, view) = drift_view(Some("essay.md"));
        let drift = block_on(check_drift_file(dir.path(), &exporter, &work, &view))
            .expect("drift check reads no repos")
            .expect("drifted file");
        assert_eq!(drift.rule_id, "AUTH_FILE_DRIFT");
        assert_eq!(drift.severity, "warning");
        assert_eq!(
            drift.message,
            "File essay.md differs from the last export of essay"
        );
        // A faithful re-export is not drift.
        let exporter = FakeExporter {
            rendered: Mutex::new("moved words".to_owned()),
            fail: false,
        };
        let drift = block_on(check_drift_file(dir.path(), &exporter, &work, &view))
            .expect("drift check reads no repos");
        assert_eq!(drift, None);
    }

    /// An exporter that always fails: the drift check propagates the error
    /// instead of reporting drift.
    struct FakeFailingExporter;

    impl DraftExporter for FakeFailingExporter {
        async fn export_markdown(&self, _work_id: Uuid) -> Result<String> {
            Err(Error::Storage("export broke".to_owned()))
        }
    }

    #[test]
    fn test_drift_propagates_an_export_failure() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("essay.md"), "moved words").expect("fixture file");
        let (work, view) = drift_view(Some("essay.md"));

        let error = block_on(check_drift_file(
            dir.path(),
            &FakeFailingExporter,
            &work,
            &view,
        ))
        .expect_err("failed export fails the check");
        assert_eq!(error.to_string(), "database or storage error: export broke");
    }

    struct FakeRevisions {
        by_id: Mutex<HashMap<Uuid, WorkRevision>>,
        latest: Mutex<Option<WorkRevision>>,
        fail_get: bool,
        fail_latest: bool,
    }

    impl FakeRevisions {
        fn empty() -> Self {
            Self {
                by_id: Mutex::new(HashMap::new()),
                latest: Mutex::new(None),
                fail_get: false,
                fail_latest: false,
            }
        }
    }

    impl WorkRevisionRepo for FakeRevisions {
        type Tx = ();
        async fn insert(
            &self,
            _tx: &mut Self::Tx,
            draft: marginalia_types::works::WorkRevisionDraft,
        ) -> Result<WorkRevision> {
            let revision = WorkRevision {
                id: Uuid::new_v4(),
                work_id: draft.work_id,
                revision_number: draft.revision_number,
                parent_revision_id: draft.parent_revision_id,
                state: RevisionState::Draft,
                message: draft.message,
                content_hash: None,
                created_by: draft.created_by,
                created_at: Utc::now(),
                frozen_at: None,
                published_at: None,
                metadata: draft.metadata,
            };
            self.by_id
                .lock()
                .expect("fake lock")
                .insert(revision.id, revision.clone());
            Ok(revision)
        }
        async fn get(&self, revision_id: Uuid) -> Result<Option<WorkRevision>> {
            if self.fail_get {
                return Err(Error::Storage("revisions broke".to_owned()));
            }
            Ok(self
                .by_id
                .lock()
                .expect("fake lock")
                .get(&revision_id)
                .cloned())
        }
        async fn latest(&self, _work_id: Uuid) -> Result<Option<WorkRevision>> {
            if self.fail_latest {
                return Err(Error::Storage("revisions broke".to_owned()));
            }
            Ok(self.latest.lock().expect("fake lock").clone())
        }
        async fn copy_forward(
            &self,
            _tx: &mut Self::Tx,
            revision_id: Uuid,
        ) -> Result<WorkRevision> {
            let source = self
                .by_id
                .lock()
                .expect("fake lock")
                .get(&revision_id)
                .cloned()
                .ok_or_else(|| Error::NotFound {
                    kind: "work_revision",
                    id: revision_id.to_string(),
                })?;
            let next = WorkRevision {
                id: Uuid::new_v4(),
                work_id: source.work_id,
                revision_number: source.revision_number + 1,
                parent_revision_id: Some(source.id),
                state: RevisionState::Draft,
                message: None,
                content_hash: None,
                created_by: source.created_by.clone(),
                created_at: Utc::now(),
                frozen_at: None,
                published_at: None,
                metadata: source.metadata.clone(),
            };
            self.by_id
                .lock()
                .expect("fake lock")
                .insert(next.id, next.clone());
            *self.latest.lock().expect("fake lock") = Some(next.clone());
            Ok(next)
        }
        async fn set_message(
            &self,
            _tx: &mut Self::Tx,
            revision_id: Uuid,
            message: &str,
        ) -> Result<WorkRevision> {
            let mut by_id = self.by_id.lock().expect("fake lock");
            let Some(revision) = by_id.get_mut(&revision_id) else {
                return Err(Error::NotFound {
                    kind: "work_revision",
                    id: revision_id.to_string(),
                });
            };
            revision.message = Some(message.to_owned());
            Ok(revision.clone())
        }
        async fn freeze(
            &self,
            _tx: &mut Self::Tx,
            revision_id: Uuid,
            content_hash: &[u8],
        ) -> Result<WorkRevision> {
            let mut by_id = self.by_id.lock().expect("fake lock");
            let Some(revision) = by_id.get_mut(&revision_id) else {
                return Err(Error::NotFound {
                    kind: "work_revision",
                    id: revision_id.to_string(),
                });
            };
            revision.state = RevisionState::Frozen;
            revision.content_hash = Some(content_hash.to_vec());
            revision.frozen_at = Some(Utc::now());
            Ok(revision.clone())
        }
        async fn publish(&self, _tx: &mut Self::Tx, revision_id: Uuid) -> Result<WorkRevision> {
            let mut by_id = self.by_id.lock().expect("fake lock");
            let Some(revision) = by_id.get_mut(&revision_id) else {
                return Err(Error::NotFound {
                    kind: "work_revision",
                    id: revision_id.to_string(),
                });
            };
            revision.state = RevisionState::Published;
            revision.published_at = Some(Utc::now());
            Ok(revision.clone())
        }
        async fn supersede(&self, _tx: &mut Self::Tx, revision_id: Uuid) -> Result<WorkRevision> {
            let mut by_id = self.by_id.lock().expect("fake lock");
            let Some(revision) = by_id.get_mut(&revision_id) else {
                return Err(Error::NotFound {
                    kind: "work_revision",
                    id: revision_id.to_string(),
                });
            };
            revision.state = RevisionState::Superseded;
            Ok(revision.clone())
        }
    }

    fn chained_work() -> Work {
        let mut work = make_work();
        work.current_revision_id = Some(block_uuid(0x0003_0003_0003_0003_0003_0003_0003_0003));
        work
    }

    fn chained_revisions() -> FakeRevisions {
        let rev = |n: i64, id: Uuid, parent: Option<Uuid>| {
            let mut revision = make_revision(RevisionState::Frozen, None);
            revision.id = id;
            revision.revision_number = n;
            revision.parent_revision_id = parent;
            (id, revision)
        };
        let (id1, rev1) = rev(
            1,
            block_uuid(0x0001_0001_0001_0001_0001_0001_0001_0001),
            None,
        );
        let (id2, rev2) = rev(
            2,
            block_uuid(0x0002_0002_0002_0002_0002_0002_0002_0002),
            Some(id1),
        );
        let (id3, rev3) = rev(
            3,
            block_uuid(0x0003_0003_0003_0003_0003_0003_0003_0003),
            Some(id2),
        );
        FakeRevisions {
            by_id: Mutex::new(HashMap::from([
                (id1, rev1),
                (id2, rev2),
                (id3, rev3.clone()),
            ])),
            latest: Mutex::new(Some(rev3)),
            fail_get: false,
            fail_latest: false,
        }
    }
    #[test]
    fn test_waiver_subject_falls_back_to_block_key() {
        // Without a citation key the block key names the waiver subject,
        // in both the flagging and the blocking passes.
        let mut findings = vec![check_finding(
            "AUTH_QUOTE_UNVERIFIED",
            "error",
            Some("b"),
            None,
        )];
        let waived = HashSet::from([("AUTH_QUOTE_UNVERIFIED".to_owned(), Some("b".to_owned()))]);
        mark_waived(&mut findings, &waived);
        assert_eq!(
            findings[0]
                .detail
                .as_ref()
                .and_then(|detail| detail.get("waived")),
            Some(&Value::Bool(true))
        );
        assert_eq!(
            validation_blockers(&findings, ValidateGate::Freeze, &waived),
            Vec::<String>::new()
        );
        assert_eq!(
            validation_blockers(&findings, ValidateGate::Freeze, &HashSet::new()),
            vec!["AUTH_QUOTE_UNVERIFIED".to_owned()]
        );
    }

    #[test]
    fn resolve_current_needs_a_pointer() {
        let mut work = make_work();
        work.current_revision_id = None;
        let error = block_on(resolve_revision_in_chain(&chained_revisions(), &work, None))
            .expect_err("no current revision");
        assert_eq!(
            error.to_string(),
            "work_revision not found: current of essay"
        );
    }

    #[test]
    fn resolve_current_missing_row() {
        let mut work = make_work();
        let missing = block_uuid(0x9999_0000_0000_0000_0000_0000_0000_0000);
        work.current_revision_id = Some(missing);
        let error = block_on(resolve_revision_in_chain(&chained_revisions(), &work, None))
            .expect_err("dangling current pointer");
        assert_eq!(
            error.to_string(),
            format!("work_revision not found: {missing}")
        );
    }

    #[test]
    fn resolve_walks_the_parent_chain() {
        let revisions = chained_revisions();
        let work = chained_work();
        let first = block_on(resolve_revision_in_chain(&revisions, &work, Some(1)))
            .expect("revision 1 resolves");
        assert_eq!(first.revision_number, 1);
        let current =
            block_on(resolve_revision_in_chain(&revisions, &work, None)).expect("current resolves");
        assert_eq!(current.revision_number, 3);
    }

    #[test]
    fn resolve_unknown_number_is_not_found() {
        let revisions = chained_revisions();
        let work = chained_work();
        let error = block_on(resolve_revision_in_chain(&revisions, &work, Some(9)))
            .expect_err("no revision 9");
        assert_eq!(
            error.to_string(),
            "work_revision not found: essay revision 9"
        );
        let empty = FakeRevisions::empty();
        let error = block_on(resolve_revision_in_chain(&empty, &work, Some(1)))
            .expect_err("no revisions at all");
        assert_eq!(
            error.to_string(),
            "work_revision not found: essay revision 1"
        );
    }

    #[test]
    fn resolve_current_get_failure_propagates() {
        let revisions = FakeRevisions {
            by_id: Mutex::new(HashMap::new()),
            latest: Mutex::new(None),
            fail_get: true,
            fail_latest: false,
        };
        let error = block_on(resolve_revision_in_chain(&revisions, &chained_work(), None))
            .expect_err("get failure fails");
        assert_eq!(
            error.to_string(),
            "database or storage error: revisions broke"
        );
    }

    #[test]
    fn resolve_latest_failure_propagates() {
        let revisions = FakeRevisions {
            by_id: Mutex::new(HashMap::new()),
            latest: Mutex::new(None),
            fail_get: false,
            fail_latest: true,
        };
        let error = block_on(resolve_revision_in_chain(
            &revisions,
            &chained_work(),
            Some(1),
        ))
        .expect_err("latest failure fails");
        assert_eq!(
            error.to_string(),
            "database or storage error: revisions broke"
        );
    }

    #[test]
    fn resolve_parent_get_failure_propagates() {
        let mut rev3 = make_revision(RevisionState::Frozen, None);
        rev3.revision_number = 3;
        rev3.parent_revision_id = Some(block_uuid(0x0002_0002_0002_0002_0002_0002_0002_0002));
        let revisions = FakeRevisions {
            by_id: Mutex::new(HashMap::from([(rev3.id, rev3.clone())])),
            latest: Mutex::new(Some(rev3)),
            fail_get: true,
            fail_latest: false,
        };
        let error = block_on(resolve_revision_in_chain(
            &revisions,
            &chained_work(),
            Some(1),
        ))
        .expect_err("parent get failure fails");
        assert_eq!(
            error.to_string(),
            "database or storage error: revisions broke"
        );
    }

    #[test]
    fn policy_license_two_stage() {
        let quota = || ValidationFinding {
            rule_id: "AUTH_LICENSE_EXPORT".to_owned(),
            severity: "warning".to_owned(),
            block_key: None,
            citation_key: None,
            message: "q".to_owned(),
            detail: None,
        };
        // The gate sets the default: error at publish, warning elsewhere.
        let mut findings = vec![quota()];
        apply_policy(
            &mut findings,
            &HashMap::new(),
            "essay",
            ValidateGate::Publish,
        );
        assert_eq!(findings[0].severity, "error");
        let mut findings = vec![quota()];
        apply_policy(
            &mut findings,
            &HashMap::new(),
            "essay",
            ValidateGate::Freeze,
        );
        assert_eq!(findings[0].severity, "warning");
        // A policy error escalates even where the gate only warns.
        let policy = HashMap::from([(
            "essay".to_owned(),
            HashMap::from([("AUTH_LICENSE_EXPORT".to_owned(), "error".to_owned())]),
        )]);
        let mut findings = vec![quota()];
        apply_policy(&mut findings, &policy, "essay", ValidateGate::None);
        assert_eq!(findings[0].severity, "error");
        // But `allow` never silences the quota: the floor holds a warning.
        let policy = HashMap::from([(
            "essay".to_owned(),
            HashMap::from([("AUTH_LICENSE_EXPORT".to_owned(), "allow".to_owned())]),
        )]);
        let mut findings = vec![quota()];
        apply_policy(&mut findings, &policy, "essay", ValidateGate::None);
        assert_eq!(findings[0].severity, "warning");
    }

    #[test]
    fn policy_allow_drops_ordinary_findings() {
        let policy = HashMap::from([(
            "essay".to_owned(),
            HashMap::from([("AUTH_SPAN_REGION".to_owned(), "allow".to_owned())]),
        )]);
        let mut findings = vec![check_finding(
            "AUTH_SPAN_REGION",
            "warning",
            Some("b"),
            Some("c"),
        )];
        apply_policy(&mut findings, &policy, "essay", ValidateGate::None);
        assert_eq!(findings, vec![]);
    }

    #[test]
    fn waived_findings_carry_the_flag() {
        let mut findings = vec![
            check_finding("AUTH_QUOTE_UNVERIFIED", "error", Some("b"), Some("c1")),
            check_finding("AUTH_SPAN_NOT_NARROWED", "error", Some("b"), Some("c2")),
        ];
        let waived = HashSet::from([("AUTH_QUOTE_UNVERIFIED".to_owned(), Some("c1".to_owned()))]);
        mark_waived(&mut findings, &waived);
        assert_eq!(
            findings[0]
                .detail
                .as_ref()
                .and_then(|detail| detail.get("waived")),
            Some(&Value::Bool(true))
        );
        assert_eq!(findings[1].detail, None);
        // The global pair clears blockers without flagging every row.
        assert_eq!(
            validation_blockers(
                &findings,
                ValidateGate::Freeze,
                &HashSet::from([("AUTH_QUOTE_UNVERIFIED".to_owned(), None)])
            ),
            vec!["AUTH_SPAN_NOT_NARROWED".to_owned()]
        );
    }

    // --- `validate` end to end: assemble, check, drift, policy, waive, gate.

    struct FakeWorks {
        work: Mutex<Option<Work>>,
        fail_slug: bool,
    }

    impl WorkRepo for FakeWorks {
        type Tx = ();
        async fn insert(
            &self,
            _tx: &mut Self::Tx,
            draft: marginalia_types::works::WorkDraft,
        ) -> Result<Work> {
            let now = Utc::now();
            let work = Work {
                id: Uuid::new_v4(),
                slug: draft.slug,
                title: draft.title,
                work_type: draft.work_type,
                status: WorkStatus::Draft,
                language: draft.language,
                abstract_text: draft.abstract_text,
                current_revision_id: None,
                metadata: draft.metadata,
                created_at: now,
                updated_at: now,
                archived_at: None,
            };
            *self.work.lock().expect("fake lock") = Some(work.clone());
            Ok(work)
        }
        async fn get(&self, work_id: Uuid) -> Result<Option<Work>> {
            Ok(self
                .work
                .lock()
                .expect("fake lock")
                .clone()
                .filter(|work| work.id == work_id))
        }
        async fn get_by_slug(&self, slug: &str) -> Result<Option<Work>> {
            if self.fail_slug {
                return Err(Error::Storage("works broke".to_owned()));
            }
            Ok(self
                .work
                .lock()
                .expect("fake lock")
                .clone()
                .filter(|work| work.slug == slug))
        }
        async fn list(&self) -> Result<Vec<Work>> {
            Ok(self
                .work
                .lock()
                .expect("fake lock")
                .clone()
                .into_iter()
                .collect())
        }
        async fn set_current_revision(
            &self,
            _tx: &mut Self::Tx,
            work_id: Uuid,
            revision_id: Uuid,
        ) -> Result<()> {
            let mut slot = self.work.lock().expect("fake lock");
            let Some(work) = slot.as_mut().filter(|work| work.id == work_id) else {
                return Err(Error::NotFound {
                    kind: "work",
                    id: work_id.to_string(),
                });
            };
            work.current_revision_id = Some(revision_id);
            Ok(())
        }
        async fn update(
            &self,
            _tx: &mut Self::Tx,
            work_id: Uuid,
            expected_updated_at: chrono::DateTime<Utc>,
            fields: Map<String, Value>,
        ) -> Result<Work> {
            let mut slot = self.work.lock().expect("fake lock");
            let Some(work) = slot.as_mut().filter(|work| work.id == work_id) else {
                return Err(Error::NotFound {
                    kind: "work",
                    id: work_id.to_string(),
                });
            };
            if work.updated_at != expected_updated_at {
                return Err(Error::Validation(format!(
                    "work {} changed under this write",
                    work.slug
                )));
            }
            // The fake applies the known scalar fields and merges the rest
            // into metadata; only the concurrency check is contractual.
            for (key, value) in fields {
                match (key.as_str(), value) {
                    ("title", Value::String(title)) => work.title = title,
                    ("work_type", Value::String(work_type)) => work.work_type = work_type,
                    (key, value) => {
                        work.metadata.insert(key.to_owned(), value);
                    }
                }
            }
            work.updated_at = Utc::now();
            Ok(work.clone())
        }
        async fn archive(&self, _tx: &mut Self::Tx, work_id: Uuid) -> Result<Work> {
            let mut slot = self.work.lock().expect("fake lock");
            let Some(work) = slot.as_mut().filter(|work| work.id == work_id) else {
                return Err(Error::NotFound {
                    kind: "work",
                    id: work_id.to_string(),
                });
            };
            work.status = WorkStatus::Archived;
            Ok(work.clone())
        }
    }

    struct FakeBlocks {
        tree: Mutex<Vec<WorkBlock>>,
        fail_tree: bool,
    }

    impl WorkBlockRepo for FakeBlocks {
        type Tx = ();
        async fn upsert(
            &self,
            _tx: &mut Self::Tx,
            revision_id: Uuid,
            draft: marginalia_types::works::WorkBlockDraft,
            expected_updated_at: Option<chrono::DateTime<Utc>>,
        ) -> Result<WorkBlock> {
            let mut tree = self.tree.lock().expect("fake lock");
            if let Some(row) = tree
                .iter_mut()
                .find(|row| row.revision_id == revision_id && row.block_key == draft.block_key)
            {
                if expected_updated_at.is_some_and(|stamp| stamp != row.updated_at) {
                    return Err(Error::Validation(format!(
                        "block {} changed under this write",
                        draft.block_key
                    )));
                }
                row.parent_id = draft.parent_id;
                row.position = draft.position;
                row.block_type = draft.block_type;
                row.title = draft.title;
                row.body_markdown = draft.body_markdown;
                row.attributes = draft.attributes;
                row.updated_at = Utc::now();
                return Ok(row.clone());
            }
            let now = Utc::now();
            let row = WorkBlock {
                id: Uuid::new_v4(),
                revision_id,
                block_key: draft.block_key,
                parent_id: draft.parent_id,
                position: draft.position,
                block_type: draft.block_type,
                title: draft.title,
                body_markdown: draft.body_markdown,
                attributes: draft.attributes,
                created_at: now,
                updated_at: now,
            };
            tree.push(row.clone());
            Ok(row)
        }
        async fn tree(&self, _revision_id: Uuid) -> Result<Vec<WorkBlock>> {
            if self.fail_tree {
                return Err(Error::Storage("blocks broke".to_owned()));
            }
            // Blocks carry a dummy revision id in fixtures: every stored row
            // belongs to the single revision under test.
            Ok(self.tree.lock().expect("fake lock").clone())
        }
        async fn get(&self, block_id: Uuid) -> Result<Option<WorkBlock>> {
            Ok(self
                .tree
                .lock()
                .expect("fake lock")
                .iter()
                .find(|row| row.id == block_id)
                .cloned())
        }
        async fn by_key(&self, revision_id: Uuid, block_key: Uuid) -> Result<Option<WorkBlock>> {
            Ok(self
                .tree
                .lock()
                .expect("fake lock")
                .iter()
                .find(|row| row.revision_id == revision_id && row.block_key == block_key)
                .cloned())
        }
        async fn by_key_in_tx(
            &self,
            _tx: &mut Self::Tx,
            revision_id: Uuid,
            block_key: Uuid,
        ) -> Result<Option<WorkBlock>> {
            self.by_key(revision_id, block_key).await
        }
        async fn delete(&self, _tx: &mut Self::Tx, block_id: Uuid) -> Result<()> {
            self.tree
                .lock()
                .expect("fake lock")
                .retain(|row| row.id != block_id);
            Ok(())
        }
    }

    struct FakeCites {
        entries: Mutex<Vec<BlockCitations>>,
    }

    impl CitationRepo for FakeCites {
        type Tx = ();
        async fn insert_occurrence(
            &self,
            _tx: &mut Self::Tx,
            draft: marginalia_types::citations::OccurrenceDraft,
        ) -> Result<CitationOccurrence> {
            let occurrence = CitationOccurrence {
                id: Uuid::new_v4(),
                citation_key: draft.citation_key,
                block_id: draft.block_id,
                placement: draft.placement,
                intent: draft.intent,
                note: draft.note,
                created_at: Utc::now(),
            };
            self.entries
                .lock()
                .expect("fake lock")
                .push(BlockCitations {
                    occurrence: occurrence.clone(),
                    items: Vec::new(),
                });
            Ok(occurrence)
        }
        async fn insert_item(
            &self,
            _tx: &mut Self::Tx,
            draft: marginalia_types::citations::CitationItemDraft,
        ) -> Result<CitationItem> {
            let mut entries = self.entries.lock().expect("fake lock");
            let Some(entry) = entries
                .iter_mut()
                .find(|entry| entry.occurrence.id == draft.occurrence_id)
            else {
                return Err(Error::NotFound {
                    kind: "citation_occurrence",
                    id: draft.occurrence_id.to_string(),
                });
            };
            let item = CitationItem {
                occurrence_id: draft.occurrence_id,
                position: draft.position,
                edition_id: draft.edition_id,
                edition_key: draft.edition_key,
                source_span_id: draft.source_span_id,
                quoted_text: draft.quoted_text,
                verify_status: draft.verify_status,
                verified_at: None,
                locator: draft.locator,
                prefix: draft.prefix,
                suffix: draft.suffix,
                suppress_author: draft.suppress_author,
            };
            entry.items.push(item.clone());
            Ok(item)
        }
        async fn for_block(&self, block_id: Uuid) -> Result<Vec<BlockCitations>> {
            Ok(self
                .entries
                .lock()
                .expect("fake lock")
                .iter()
                .filter(|entry| entry.occurrence.block_id == block_id)
                .cloned()
                .collect())
        }
        async fn for_revision(&self, _revision_id: Uuid) -> Result<Vec<BlockCitations>> {
            // Occurrences carry no revision link in the fake: every stored row
            // belongs to the single revision under test.
            Ok(self.entries.lock().expect("fake lock").clone())
        }
        async fn by_key(
            &self,
            _revision_id: Uuid,
            citation_key: Uuid,
        ) -> Result<Option<BlockCitations>> {
            Ok(self
                .entries
                .lock()
                .expect("fake lock")
                .iter()
                .find(|entry| entry.occurrence.citation_key == citation_key)
                .cloned())
        }
        async fn citing_span(&self, span_id: Uuid) -> Result<Vec<BlockCitations>> {
            Ok(self
                .entries
                .lock()
                .expect("fake lock")
                .iter()
                .filter(|entry| {
                    entry
                        .items
                        .iter()
                        .any(|item| item.source_span_id == Some(span_id))
                })
                .cloned()
                .collect())
        }
        async fn citing_key(&self, edition_key: &str) -> Result<Vec<BlockCitations>> {
            Ok(self
                .entries
                .lock()
                .expect("fake lock")
                .iter()
                .filter(|entry| {
                    entry
                        .items
                        .iter()
                        .any(|item| item.edition_key.as_deref() == Some(edition_key))
                })
                .cloned()
                .collect())
        }
    }

    struct FakeLinks {
        sources: Mutex<Vec<marginalia_types::works::BlockSourceLink>>,
        entities: Mutex<Vec<marginalia_types::works::BlockEntityLink>>,
    }

    impl WorkLinkRepo for FakeLinks {
        type Tx = ();
        async fn add_source_link(
            &self,
            _tx: &mut Self::Tx,
            draft: marginalia_types::works::BlockSourceLinkDraft,
        ) -> Result<marginalia_types::works::BlockSourceLink> {
            let link = marginalia_types::works::BlockSourceLink {
                block_id: draft.block_id,
                source_span_id: draft.source_span_id,
                relation: draft.relation,
                confidence: draft.confidence,
                note: draft.note,
                created_at: Utc::now(),
            };
            self.sources.lock().expect("fake lock").push(link.clone());
            Ok(link)
        }
        async fn add_entity_link(
            &self,
            _tx: &mut Self::Tx,
            draft: marginalia_types::works::BlockEntityLinkDraft,
        ) -> Result<marginalia_types::works::BlockEntityLink> {
            let link = marginalia_types::works::BlockEntityLink {
                block_id: draft.block_id,
                entity_id: draft.entity_id,
                relation: draft.relation,
                surface_form: draft.surface_form,
                created_at: Utc::now(),
            };
            self.entities.lock().expect("fake lock").push(link.clone());
            Ok(link)
        }
        async fn for_block(&self, block_id: Uuid) -> Result<BlockLinks> {
            Ok(BlockLinks {
                sources: self
                    .sources
                    .lock()
                    .expect("fake lock")
                    .iter()
                    .filter(|link| link.block_id == block_id)
                    .cloned()
                    .collect(),
                entities: self
                    .entities
                    .lock()
                    .expect("fake lock")
                    .iter()
                    .filter(|link| link.block_id == block_id)
                    .cloned()
                    .collect(),
            })
        }
        async fn for_span(
            &self,
            span_id: Uuid,
        ) -> Result<Vec<marginalia_types::works::BlockSourceLink>> {
            Ok(self
                .sources
                .lock()
                .expect("fake lock")
                .iter()
                .filter(|link| link.source_span_id == span_id)
                .cloned()
                .collect())
        }
        async fn for_entity(
            &self,
            entity_id: Uuid,
            relation: &str,
        ) -> Result<Vec<marginalia_types::works::BlockEntityLink>> {
            Ok(self
                .entities
                .lock()
                .expect("fake lock")
                .iter()
                .filter(|link| link.entity_id == entity_id && link.relation == relation)
                .cloned()
                .collect())
        }
    }

    struct FakeSpans {
        spans: Mutex<HashMap<Uuid, SourceSpan>>,
    }

    impl SourceSpanRepo for FakeSpans {
        type Tx = ();
        async fn resolve(
            &self,
            _tx: &mut Self::Tx,
            document_id: Uuid,
            char_start: i64,
            char_end: i64,
        ) -> Result<SourceSpan> {
            // The fake cannot slice canonical text: the quote starts empty
            // and the row is retrievable like any other span.
            let span = SourceSpan {
                id: Uuid::new_v4(),
                document_id,
                char_start,
                char_end,
                quoted_text: String::new(),
                parser: None,
                parser_version: None,
                passage_id: None,
                created_at: Utc::now(),
            };
            self.spans
                .lock()
                .expect("fake lock")
                .insert(span.id, span.clone());
            Ok(span)
        }
        async fn get(&self, span_id: Uuid) -> Result<Option<SourceSpan>> {
            Ok(self.spans.lock().expect("fake lock").get(&span_id).cloned())
        }
        async fn for_document(&self, document_id: Uuid) -> Result<Vec<SourceSpan>> {
            Ok(self
                .spans
                .lock()
                .expect("fake lock")
                .values()
                .filter(|span| span.document_id == document_id)
                .cloned()
                .collect())
        }
        async fn stale(&self, limit: i64) -> Result<Vec<SourceSpan>> {
            // Stale means never re-verified: no parser version recorded.
            let mut rows: Vec<SourceSpan> = self
                .spans
                .lock()
                .expect("fake lock")
                .values()
                .filter(|span| span.parser_version.is_none())
                .cloned()
                .collect();
            rows.sort_by_key(|span| span.id);
            rows.truncate(limit.max(0) as usize);
            Ok(rows)
        }
    }

    struct FakeWaivers {
        rows: Mutex<Vec<Waiver>>,
        fail: bool,
    }

    impl WaiverRepo for FakeWaivers {
        type Tx = ();
        async fn insert(
            &self,
            _tx: &mut Self::Tx,
            draft: marginalia_types::works::WaiverDraft,
        ) -> Result<Waiver> {
            let waiver = Waiver {
                id: Uuid::new_v4(),
                revision_id: draft.revision_id,
                rule_id: draft.rule_id,
                subject: draft.subject,
                actor: draft.actor,
                reason: draft.reason,
                created_at: Utc::now(),
            };
            self.rows.lock().expect("fake lock").push(waiver.clone());
            Ok(waiver)
        }
        async fn for_revision(&self, revision_id: Uuid) -> Result<Vec<Waiver>> {
            if self.fail {
                return Err(Error::Storage("waivers broke".to_owned()));
            }
            Ok(self
                .rows
                .lock()
                .expect("fake lock")
                .iter()
                .filter(|waiver| waiver.revision_id == revision_id)
                .cloned()
                .collect())
        }
    }

    fn waiver_row(rule_id: &str, subject: Option<&str>) -> Waiver {
        Waiver {
            id: block_uuid(0x7777_7777_7777_7777_7777_7777_7777_7777),
            revision_id: block_uuid(0x4444_4444_4444_4444_4444_4444_4444_4444),
            rule_id: rule_id.to_owned(),
            subject: subject.map(str::to_owned),
            actor: "user".to_owned(),
            reason: "earned".to_owned(),
            created_at: Utc::now(),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn make_service(
        work: Work,
        revision: WorkRevision,
        blocks: Vec<WorkBlock>,
        entries: Vec<BlockCitations>,
        spans: HashMap<Uuid, SourceSpan>,
        corpus: Corpus,
        waivers: Vec<Waiver>,
        works_dir: Option<PathBuf>,
        rendered: &str,
    ) -> WorkValidationService<
        FakeWorks,
        FakeRevisions,
        FakeBlocks,
        FakeCites,
        FakeLinks,
        FakeEditions,
        FakeWaivers,
        FakeSpans,
        FakeDocs,
        FakeTexts,
        FakePassages,
        FakeExporter,
    > {
        make_service_with_exporter(
            work,
            revision,
            blocks,
            entries,
            spans,
            corpus,
            waivers,
            works_dir,
            FakeExporter {
                rendered: Mutex::new(rendered.to_owned()),
                fail: false,
            },
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn make_service_with_exporter(
        work: Work,
        revision: WorkRevision,
        blocks: Vec<WorkBlock>,
        entries: Vec<BlockCitations>,
        spans: HashMap<Uuid, SourceSpan>,
        corpus: Corpus,
        waivers: Vec<Waiver>,
        works_dir: Option<PathBuf>,
        exporter: FakeExporter,
    ) -> WorkValidationService<
        FakeWorks,
        FakeRevisions,
        FakeBlocks,
        FakeCites,
        FakeLinks,
        FakeEditions,
        FakeWaivers,
        FakeSpans,
        FakeDocs,
        FakeTexts,
        FakePassages,
        FakeExporter,
    > {
        let revision_id = revision.id;
        WorkValidationService::new(
            FakeWorks {
                work: Mutex::new(Some(work)),
                fail_slug: corpus.fail_works,
            },
            FakeRevisions {
                by_id: Mutex::new(HashMap::from([(revision_id, revision.clone())])),
                latest: Mutex::new(Some(revision)),
                fail_get: corpus.fail_revisions,
                fail_latest: corpus.fail_revisions,
            },
            FakeBlocks {
                tree: Mutex::new(blocks),
                fail_tree: corpus.fail_blocks,
            },
            FakeCites {
                entries: Mutex::new(entries),
            },
            FakeLinks {
                sources: Mutex::new(Vec::new()),
                entities: Mutex::new(Vec::new()),
            },
            FakeEditions {
                editions: editions_store(corpus.edition_keys, corpus.editions_by_id),
                fail_list: corpus.fail_edition_list,
                fail_get: corpus.fail_edition_get,
            },
            FakeWaivers {
                rows: Mutex::new(waivers),
                fail: corpus.fail_waivers,
            },
            FakeSpans {
                spans: Mutex::new(spans),
            },
            FakeDocs {
                docs: docs_store(corpus.docs),
                fail: corpus.fail_docs,
            },
            FakeTexts {
                texts: texts_store(corpus.versions),
                fail_versions: corpus.fail_versions,
            },
            FakePassages {
                regions: corpus.regions,
                stored: Mutex::new(HashMap::new()),
                embeddings: Mutex::new(HashMap::new()),
                fail_covering: corpus.fail_covering,
            },
            HashMap::new(),
            works_dir,
            Some(exporter),
        )
    }

    #[test]
    fn validate_clean_paragraph_passes_none() {
        let mut work = make_work();
        let revision = make_revision(RevisionState::Draft, None);
        work.current_revision_id = Some(revision.id);
        let block = make_block(
            block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb),
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            "plain words",
        );
        let service = make_service(
            work,
            revision,
            vec![block],
            vec![],
            HashMap::new(),
            Corpus::default(),
            vec![],
            None,
            "",
        );
        let report = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect("clean revision validates");
        assert_eq!(report.work, "essay");
        assert_eq!(report.revision_number, 1);
        assert_eq!(report.state, "draft");
        assert_eq!(report.findings, vec![]);
        assert_eq!(report.citations, vec![]);
        assert!(report.gate.passed);
        assert_eq!(report.gate.blockers, Vec::<String>::new());
    }

    #[test]
    fn validate_unknown_slug_is_not_found() {
        let empty = WorkValidationService::new(
            FakeWorks {
                work: Mutex::new(None),
                fail_slug: false,
            },
            FakeRevisions::empty(),
            FakeBlocks {
                tree: Mutex::new(vec![]),
                fail_tree: false,
            },
            FakeCites {
                entries: Mutex::new(vec![]),
            },
            FakeLinks {
                sources: Mutex::new(Vec::new()),
                entities: Mutex::new(Vec::new()),
            },
            FakeEditions {
                editions: editions_store(HashSet::new(), HashMap::new()),
                fail_list: false,
                fail_get: false,
            },
            FakeWaivers {
                rows: Mutex::new(vec![]),
                fail: false,
            },
            FakeSpans {
                spans: Mutex::new(HashMap::new()),
            },
            FakeDocs {
                docs: Mutex::new(HashMap::new()),
                fail: false,
            },
            FakeTexts {
                texts: Mutex::new(HashMap::new()),
                fail_versions: false,
            },
            FakePassages {
                regions: vec![],
                stored: Mutex::new(HashMap::new()),
                embeddings: Mutex::new(HashMap::new()),
                fail_covering: false,
            },
            HashMap::new(),
            None,
            None::<FakeExporter>,
        );
        let error = block_on(empty.validate("missing", None, ValidateGate::None, None))
            .expect_err("unknown slug");
        assert_eq!(error.to_string(), "work not found: missing");
    }

    #[test]
    fn validate_publish_edition_missing_blocks_until_waived() {
        let cite_key = block_uuid(0xaaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa);
        let block_id = block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb);
        let occurrence_id = block_uuid(0xeeee_eeee_eeee_eeee_eeee_eeee_eeee_eeee);
        let mut work = make_work();
        let revision = make_revision(RevisionState::Draft, None);
        work.current_revision_id = Some(revision.id);
        let block = make_block(
            block_id,
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            &crate::markers::format_marker(&cite_key),
        );
        let occurrence = make_occurrence(occurrence_id, cite_key, block_id, Intent::Support);
        let entries = vec![BlockCitations {
            occurrence,
            items: vec![make_item(occurrence_id, 0, None, None, None, None, None)],
        }];
        // At publish the missing edition blocks.
        let service = make_service(
            work.clone(),
            revision.clone(),
            vec![block.clone()],
            entries.clone(),
            HashMap::new(),
            Corpus::default(),
            vec![],
            None,
            "",
        );
        let report = block_on(service.validate("essay", None, ValidateGate::Publish, None))
            .expect("publish validates");
        assert!(!report.gate.passed);
        assert_eq!(
            report.gate.blockers,
            vec!["AUTH_CITATION_EDITION_MISSING".to_owned()]
        );
        // A prospective waiver for the citation clears the blocker and flags
        // the finding, while the bibliography warning still lists.
        let prospective = HashSet::from([(
            "AUTH_CITATION_EDITION_MISSING".to_owned(),
            Some(cite_key.to_string()),
        )]);
        let service = make_service(
            work,
            revision,
            vec![block],
            entries,
            HashMap::new(),
            Corpus::default(),
            vec![],
            None,
            "",
        );
        let report =
            block_on(service.validate("essay", None, ValidateGate::Publish, Some(&prospective)))
                .expect("waived publish validates");
        assert!(report.gate.passed);
        assert_eq!(report.gate.blockers, Vec::<String>::new());
        let missing = finding(&report.findings, "AUTH_CITATION_EDITION_MISSING");
        assert_eq!(
            missing
                .detail
                .as_ref()
                .and_then(|detail| detail.get("waived")),
            Some(&Value::Bool(true))
        );
        assert_eq!(report.citations.len(), 1);
        assert_eq!(report.citations[0].citation_key, cite_key.to_string());
    }

    #[test]
    fn validate_stored_waiver_clears_freeze() {
        let cite_key = block_uuid(0xaaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa);
        let block_id = block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb);
        let occurrence_id = block_uuid(0xeeee_eeee_eeee_eeee_eeee_eeee_eeee_eeee);
        let span_id = block_uuid(0xcccc_cccc_cccc_cccc_cccc_cccc_cccc_cccc);
        let mut work = make_work();
        let revision = make_revision(RevisionState::Draft, None);
        work.current_revision_id = Some(revision.id);
        let block = make_block(
            block_id,
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            &crate::markers::format_marker(&cite_key),
        );
        let occurrence = make_occurrence(occurrence_id, cite_key, block_id, Intent::Quotation);
        // A near quote is an error the stored waiver earns back.
        let entries = vec![BlockCitations {
            occurrence,
            items: vec![make_item(
                occurrence_id,
                0,
                Some("ED1"),
                None,
                Some(span_id),
                Some("a fine sentence here"),
                Some("near"),
            )],
        }];
        let spans = HashMap::from([(span_id, make_span(span_id, 10, 60, Some("pv2")))]);
        let service = make_service(
            work.clone(),
            revision.clone(),
            vec![block.clone()],
            entries.clone(),
            spans.clone(),
            Corpus::default(),
            vec![waiver_row(
                "AUTH_QUOTE_UNVERIFIED",
                Some(&cite_key.to_string()),
            )],
            None,
            "",
        );
        let report = block_on(service.validate("essay", None, ValidateGate::Freeze, None))
            .expect("waived freeze validates");
        assert!(report.gate.passed);
        assert_eq!(report.gate.blockers, Vec::<String>::new());
        assert_eq!(
            finding(&report.findings, "AUTH_QUOTE_UNVERIFIED")
                .detail
                .as_ref()
                .and_then(|detail| detail.get("waived")),
            Some(&Value::Bool(true))
        );
    }

    #[test]
    fn validate_current_draft_detects_drift() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("essay.md"), "moved words").expect("fixture file");
        let mut work = make_work();
        let mut revision = make_revision(RevisionState::Draft, None);
        let mut port = Map::new();
        port.insert("file".to_owned(), Value::String("essay.md".to_owned()));
        let mut metadata = Map::new();
        metadata.insert("port".to_owned(), Value::Object(port));
        revision.metadata = metadata;
        work.current_revision_id = Some(revision.id);
        let block = make_block(
            block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb),
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            "plain words",
        );
        let service = make_service(
            work,
            revision,
            vec![block],
            vec![],
            HashMap::new(),
            Corpus::default(),
            vec![],
            Some(dir.path().to_owned()),
            "rendered",
        );
        let report = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect("drifted draft validates");
        // Gate none lists and passes, drift included.
        assert!(report.gate.passed);
        let drift = finding(&report.findings, "AUTH_FILE_DRIFT");
        assert_eq!(
            drift.message,
            "File essay.md differs from the last export of essay"
        );
    }

    type FailingService = WorkValidationService<
        FakeWorks,
        FakeRevisions,
        FakeBlocks,
        FakeCites,
        FakeLinks,
        FakeEditions,
        FakeWaivers,
        FakeSpans,
        FakeDocs,
        FakeTexts,
        FakePassages,
        FakeExporter,
    >;

    /// A clean paragraph revision behind `validate`, for failures past
    /// assembly: no spans, so only the always-read corpus calls run.
    fn paragraph_service(corpus: Corpus) -> FailingService {
        let mut work = make_work();
        let revision = make_revision(RevisionState::Draft, None);
        work.current_revision_id = Some(revision.id);
        let block = make_block(
            block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb),
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            "plain words",
        );
        make_service(
            work,
            revision,
            vec![block],
            vec![],
            HashMap::new(),
            corpus,
            vec![],
            None,
            "",
        )
    }

    /// Rows of the clean occurrence for service-level tests: the stored
    /// block, its citation entry, the span map, and the ids an item rebuild
    /// needs.
    fn cited_rows() -> (
        WorkBlock,
        BlockCitations,
        HashMap<Uuid, SourceSpan>,
        Uuid,
        Uuid,
    ) {
        let (_, view, _, span_id) = clean_fixture();
        let occurrence_id = view.blocks[0].citations[0].occurrence.id;
        (
            view.blocks[0].block.clone(),
            view.blocks[0].citations[0].clone(),
            view.spans.clone(),
            occurrence_id,
            span_id,
        )
    }

    /// The clean occurrence behind `validate`, for failures inside the
    /// per-item checks: the span gives prime corpus reads to run against.
    fn cited_service(
        corpus: Corpus,
        entry: BlockCitations,
        spans: HashMap<Uuid, SourceSpan>,
    ) -> FailingService {
        let (block, _, _, _, _) = cited_rows();
        let mut work = make_work();
        let revision = make_revision(RevisionState::Draft, None);
        work.current_revision_id = Some(revision.id);
        make_service(
            work,
            revision,
            vec![block],
            vec![entry],
            spans,
            corpus,
            vec![],
            None,
            "",
        )
    }

    #[test]
    fn validate_propagates_a_works_failure() {
        let corpus = Corpus {
            fail_works: true,
            ..Default::default()
        };
        let service = paragraph_service(corpus);
        let error = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect_err("works failure fails");
        assert_eq!(error.to_string(), "database or storage error: works broke");
        let error =
            block_on(service.validate_for_gate("essay", ValidateGate::None, &HashSet::new()))
                .expect_err("gate reports the failure too");
        assert_eq!(error.to_string(), "database or storage error: works broke");
    }

    #[test]
    fn validate_propagates_a_revision_failure() {
        let corpus = Corpus {
            fail_revisions: true,
            ..Default::default()
        };
        let service = paragraph_service(corpus);
        let error = block_on(service.validate("essay", Some(1), ValidateGate::None, None))
            .expect_err("revision failure fails");
        assert_eq!(
            error.to_string(),
            "database or storage error: revisions broke"
        );
    }

    #[test]
    fn validate_propagates_an_assembly_failure() {
        let corpus = Corpus {
            fail_blocks: true,
            ..Default::default()
        };
        let service = paragraph_service(corpus);
        let error = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect_err("assembly failure fails");
        assert_eq!(error.to_string(), "database or storage error: blocks broke");
    }

    #[test]
    fn validate_propagates_a_document_failure() {
        let (_, entry, spans, _, _) = cited_rows();
        let corpus = Corpus {
            fail_docs: true,
            ..Default::default()
        };
        let service = cited_service(corpus, entry, spans);
        let error = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect_err("document failure fails");
        assert_eq!(error.to_string(), "database or storage error: docs broke");
    }

    #[test]
    fn validate_propagates_a_texts_failure() {
        let (_, entry, spans, _, _) = cited_rows();
        let corpus = Corpus {
            fail_versions: true,
            ..Default::default()
        };
        let service = cited_service(corpus, entry, spans);
        let error = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect_err("versions failure fails");
        assert_eq!(error.to_string(), "database or storage error: texts broke");
    }

    #[test]
    fn validate_propagates_an_edition_list_failure() {
        let corpus = Corpus {
            fail_edition_list: true,
            ..Default::default()
        };
        let service = paragraph_service(corpus);
        let error = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect_err("edition list failure fails");
        assert_eq!(
            error.to_string(),
            "database or storage error: editions broke"
        );
    }

    #[test]
    fn validate_propagates_an_edition_get_failure() {
        let (block, mut entry, spans, occurrence_id, span_id) = cited_rows();
        let edition_id = block_uuid(0xeeee_0000_0000_0000_0000_0000_0000_0000);
        let corpus = Corpus {
            editions_by_id: HashMap::from([(edition_id, "ED1".to_owned())]),
            fail_edition_get: true,
            ..Default::default()
        };
        entry.items = vec![make_item(
            occurrence_id,
            0,
            None,
            Some(edition_id),
            Some(span_id),
            Some("a fine sentence here"),
            Some("exact"),
        )];
        let mut work = make_work();
        let revision = make_revision(RevisionState::Draft, None);
        work.current_revision_id = Some(revision.id);
        let service = make_service(
            work,
            revision,
            vec![block],
            vec![entry],
            spans,
            corpus,
            vec![],
            None,
            "",
        );
        let error = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect_err("edition get failure fails");
        assert_eq!(
            error.to_string(),
            "database or storage error: editions broke"
        );
    }

    #[test]
    fn validate_propagates_a_covering_failure() {
        let (_, entry, spans, _, _) = cited_rows();
        let corpus = Corpus {
            fail_covering: true,
            ..Default::default()
        };
        let service = cited_service(corpus, entry, spans);
        let error = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect_err("covering failure fails");
        assert_eq!(
            error.to_string(),
            "database or storage error: passages broke"
        );
    }

    #[test]
    fn validate_propagates_a_waiver_failure() {
        let corpus = Corpus {
            fail_waivers: true,
            ..Default::default()
        };
        let service = paragraph_service(corpus);
        let error = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect_err("waiver failure fails");
        assert_eq!(
            error.to_string(),
            "database or storage error: waivers broke"
        );
    }

    #[test]
    fn validate_propagates_a_drift_export_failure() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("essay.md"), "moved words").expect("fixture file");
        let mut work = make_work();
        let mut revision = make_revision(RevisionState::Draft, None);
        let mut port = Map::new();
        port.insert("file".to_owned(), Value::String("essay.md".to_owned()));
        let mut metadata = Map::new();
        metadata.insert("port".to_owned(), Value::Object(port));
        revision.metadata = metadata;
        work.current_revision_id = Some(revision.id);
        let block = make_block(
            block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb),
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            "plain words",
        );
        let service = make_service_with_exporter(
            work,
            revision,
            vec![block],
            vec![],
            HashMap::new(),
            Corpus::default(),
            vec![],
            Some(dir.path().to_owned()),
            FakeExporter {
                rendered: Mutex::new("rendered".to_owned()),
                fail: true,
            },
        );
        let error = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect_err("export failure fails the drift check");
        assert_eq!(error.to_string(), "database or storage error: export broke");
    }

    #[test]
    fn validate_matching_export_has_no_drift() {
        // The drift check runs — works dir, exporter, current revision — and
        // reports nothing when the file matches the last export.
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("essay.md"), "rendered").expect("fixture file");
        let mut work = make_work();
        let mut revision = make_revision(RevisionState::Draft, None);
        let mut port = Map::new();
        port.insert("file".to_owned(), Value::String("essay.md".to_owned()));
        let mut metadata = Map::new();
        metadata.insert("port".to_owned(), Value::Object(port));
        revision.metadata = metadata;
        work.current_revision_id = Some(revision.id);
        let block = make_block(
            block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb),
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            "plain words",
        );
        let service = make_service(
            work,
            revision,
            vec![block],
            vec![],
            HashMap::new(),
            Corpus::default(),
            vec![],
            Some(dir.path().to_owned()),
            "rendered",
        );
        let report = block_on(service.validate("essay", None, ValidateGate::None, None))
            .expect("matching export validates");
        assert!(report.gate.passed);
        assert!(
            !rules(&report.findings).contains(&"AUTH_FILE_DRIFT"),
            "{:?}",
            report.findings
        );
    }

    #[test]
    fn validate_for_gate_reports_pass_and_blockers() {
        let mut work = make_work();
        let revision = make_revision(RevisionState::Draft, None);
        work.current_revision_id = Some(revision.id);
        let block = make_block(
            block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb),
            block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff),
            "paragraph",
            "plain words",
        );
        let service = make_service(
            work,
            revision,
            vec![block],
            vec![],
            HashMap::new(),
            Corpus::default(),
            vec![],
            None,
            "",
        );
        let gate =
            block_on(service.validate_for_gate("essay", ValidateGate::None, &HashSet::new()))
                .expect("gate report validates");
        assert!(gate.passed);
        assert_eq!(gate.blockers, Vec::<String>::new());
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
    fn test_revision_states_report_their_values() {
        assert_eq!(revision_state_value(&RevisionState::Draft), "draft");
        assert_eq!(revision_state_value(&RevisionState::Frozen), "frozen");
        assert_eq!(revision_state_value(&RevisionState::Published), "published");
        assert_eq!(
            revision_state_value(&RevisionState::Superseded),
            "superseded"
        );
    }

    #[test]
    fn test_metadata_scalar_spells_every_json_shape() {
        assert_eq!(
            metadata_scalar_text(&Value::String("ED1".to_owned())),
            "ED1"
        );
        assert_eq!(metadata_scalar_text(&Value::Bool(true)), "True");
        assert_eq!(metadata_scalar_text(&Value::Bool(false)), "False");
        assert_eq!(metadata_scalar_text(&Value::Null), "None");
        assert_eq!(metadata_scalar_text(&Value::from(5)), "5");
    }

    #[test]
    fn test_resolve_broken_parent_link_is_not_found() {
        let mut rev3 = make_revision(RevisionState::Frozen, None);
        rev3.revision_number = 3;
        rev3.parent_revision_id = Some(block_uuid(0x0002_0002_0002_0002_0002_0002_0002_0002));
        let revisions = FakeRevisions {
            by_id: Mutex::new(HashMap::from([(rev3.id, rev3.clone())])),
            latest: Mutex::new(Some(rev3)),
            fail_get: false,
            fail_latest: false,
        };
        let error = block_on(resolve_revision_in_chain(
            &revisions,
            &chained_work(),
            Some(1),
        ))
        .expect_err("dangling parent link");
        assert_eq!(
            error.to_string(),
            "work_revision not found: essay revision 1"
        );
    }

    /// An exporter that removes the drift file mid-export: the `is_file`
    /// check passed, but the read below must still fail loudly.
    struct FakeDeletingExporter {
        path: PathBuf,
    }

    impl DraftExporter for FakeDeletingExporter {
        async fn export_markdown(&self, _work_id: Uuid) -> Result<String> {
            std::fs::remove_file(&self.path).expect("drift file exists");
            Ok("rendered".to_owned())
        }
    }

    #[test]
    fn test_drift_unreadable_file_is_a_storage_error() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("essay.md");
        std::fs::write(&path, "moved words").expect("fixture file");
        let exporter = FakeDeletingExporter { path };
        let (work, view) = drift_view(Some("essay.md"));

        let error = block_on(check_drift_file(dir.path(), &exporter, &work, &view))
            .expect_err("unreadable drift fails");

        assert!(
            error.to_string().contains("cannot read work file"),
            "{error}"
        );
    }

    #[test]
    fn test_drift_unreadable_file_fails_for_the_plain_exporter() {
        // Same loud failure as the mid-export deletion, but through the
        // plain exporter: a permission-denied file passes `is_file` and
        // fails the read, firing the read-failure arm in this instantiation.
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("essay.md");
        std::fs::write(&path, "moved words").expect("fixture file");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o000))
            .expect("fixture chmod");
        let exporter = FakeExporter {
            rendered: Mutex::new("rendered".to_owned()),
            fail: false,
        };
        let (work, view) = drift_view(Some("essay.md"));
        let error = block_on(check_drift_file(dir.path(), &exporter, &work, &view))
            .expect_err("unreadable drift fails");
        assert!(
            error.to_string().contains("cannot read work file"),
            "{error}"
        );
    }

    #[test]
    fn test_drift_skipped_without_a_works_dir() {
        let service = make_service(
            make_work(),
            make_revision(RevisionState::Draft, None),
            vec![],
            vec![],
            HashMap::new(),
            Corpus::default(),
            vec![],
            None,
            "",
        );
        let view = make_view(vec![], HashMap::new(), RevisionState::Draft, None);
        let drift =
            block_on(service.check_drift(&make_work(), &view)).expect("no dir means no drift");
        assert_eq!(drift, None);
    }

    #[test]
    fn test_fake_docs_round_trip_every_method() {
        use marginalia_types::documents::{DocumentDraft, DocumentFilter};
        let repo = FakeDocs {
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
            parser_version: "pv2".to_owned(),
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
    fn test_fake_texts_round_trip_every_method() {
        let repo = FakeTexts {
            texts: Mutex::new(HashMap::new()),
            fail_versions: false,
        };
        let id = doc_id();
        assert!(block_on(repo.get(id)).expect("get").is_none());
        assert!(block_on(repo.get_text(id)).expect("text").is_none());
        assert!(block_on(repo.lengths(id)).expect("lengths").is_none());
        assert!(block_on(repo.parser_versions(&[id]))
            .expect("versions")
            .is_empty());
        block_on(repo.put(&mut (), id, "canonical words", "test", "pv2")).expect("put stores");
        assert_eq!(block_on(repo.lengths(id)).expect("lengths"), Some((15, 15)));
        assert_eq!(
            block_on(repo.get_text(id)).expect("text"),
            Some("canonical words".to_owned())
        );
        assert_eq!(
            block_on(repo.parser_versions(&[id, Uuid::new_v4()]))
                .expect("versions")
                .get(&id),
            Some(&"pv2".to_owned())
        );
        assert!(block_on(repo.missing_document_ids(Some(10)))
            .expect("missing")
            .is_empty());
    }

    /// No-op filter extension: the fake ignores filters, so any type works.
    struct ValidateFilterStub;
    impl FilterExtension for ValidateFilterStub {
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
            narrowed.iter().map(|(row, _)| *row).collect::<Vec<_>>(),
            vec![rows[1].id]
        );
        assert_eq!(
            block_on(repo.vector_search(&[1.0, 0.0], "m", "v", None, 1))
                .expect("search")
                .len(),
            1
        );
        let keyed = block_on(repo.keyword_search("alpha", None, None, 2)).expect("search");
        assert_eq!(
            keyed.iter().map(|(row, _)| *row).collect::<Vec<_>>(),
            vec![rows[1].id, rows[0].id]
        );
        // A candidate list narrows the rows before scoring them.
        let narrowed =
            block_on(repo.keyword_search("alpha", None, Some(&[rows[0].id]), 10)).expect("search");
        assert_eq!(
            narrowed.iter().map(|(row, _)| *row).collect::<Vec<_>>(),
            vec![rows[0].id]
        );
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
        assert!(block_on(repo.index_fts(&mut (), &[rows[0].id], &["t".to_owned()], "en")).is_ok());
        assert!(block_on(repo.index_fts(&mut (), &[rows[0].id], &[] as &[String], "en")).is_err());
        // The stub's fixed outputs are its contract; the fake ignores them.
        let stub = ValidateFilterStub;
        assert_eq!(stub.filter_id(), "stub");
        assert_eq!(stub.description(), "stub");
        assert!(stub.input_schema().is_empty());
        assert_eq!(
            stub.build_clause(&Value::Null).expect("stub builds"),
            "stub"
        );
        assert_eq!(
            block_on(repo.filter_candidate_ids::<ValidateFilterStub>(&Map::new(), None))
                .expect("filter")
                .len(),
            3
        );
    }

    #[test]
    fn test_fake_editions_round_trip_every_method() {
        let repo = FakeEditions {
            editions: editions_store(HashSet::from(["ED1".to_owned()]), HashMap::new()),
            fail_list: false,
            fail_get: false,
        };
        assert_eq!(
            block_on(repo.list_keys()).expect("keys"),
            vec!["ED1".to_owned()]
        );
        assert!(block_on(repo.get_by_key("ED1")).expect("get").is_some());
        assert!(block_on(repo.get_by_key("NOPE")).expect("get").is_none());
        let mut csl = Map::new();
        csl.insert("title".to_owned(), Value::String("Dabaris".to_owned()));
        let inserted = block_on(repo.upsert_key(&mut (), "ED9", Some(csl.clone()), false))
            .expect("upsert inserts");
        assert_eq!(inserted.edition_key, "ED9");
        assert_eq!(inserted.csl, csl);
        let again = block_on(repo.upsert_key(&mut (), "ED9", None, false)).expect("upsert keeps");
        assert_eq!(again.id, inserted.id);
        assert_eq!(again.csl, csl);
        assert_eq!(
            block_on(repo.get(inserted.id))
                .expect("get")
                .expect("present")
                .edition_key,
            "ED9"
        );
        assert!(block_on(repo.get(Uuid::new_v4())).expect("get").is_none());
        assert_eq!(
            block_on(repo.list_keys()).expect("keys"),
            vec!["ED1".to_owned(), "ED9".to_owned()]
        );
    }

    #[test]
    fn test_fake_revisions_round_trip_every_method() {
        use marginalia_types::works::WorkRevisionDraft;
        let repo = FakeRevisions::empty();
        let work_id = make_work().id;
        assert!(block_on(repo.get(Uuid::new_v4())).expect("get").is_none());
        assert!(block_on(repo.latest(work_id)).expect("latest").is_none());
        let revision = block_on(repo.insert(
            &mut (),
            WorkRevisionDraft {
                work_id,
                revision_number: 1,
                parent_revision_id: None,
                message: Some("start".to_owned()),
                created_by: "user".to_owned(),
                metadata: Map::new(),
            },
        ))
        .expect("insert stores");
        assert_eq!(revision.state, RevisionState::Draft);
        assert_eq!(revision.message.as_deref(), Some("start"));
        // Inserts never move the latest pointer; only copies do.
        assert!(block_on(repo.latest(work_id)).expect("latest").is_none());
        let next = block_on(repo.copy_forward(&mut (), revision.id)).expect("copy forwards");
        assert_eq!(next.revision_number, 2);
        assert_eq!(next.parent_revision_id, Some(revision.id));
        assert_eq!(next.state, RevisionState::Draft);
        assert_eq!(
            block_on(repo.latest(work_id))
                .expect("latest")
                .expect("present")
                .id,
            next.id
        );
        let noted = block_on(repo.set_message(&mut (), next.id, "hello")).expect("message sets");
        assert_eq!(noted.message.as_deref(), Some("hello"));
        let frozen = block_on(repo.freeze(&mut (), next.id, &[9u8])).expect("freeze seals");
        assert_eq!(frozen.state, RevisionState::Frozen);
        assert_eq!(frozen.content_hash, Some(vec![9u8]));
        assert!(frozen.frozen_at.is_some());
        let published = block_on(repo.publish(&mut (), next.id)).expect("publish seals");
        assert_eq!(published.state, RevisionState::Published);
        assert!(published.published_at.is_some());
        let superseded = block_on(repo.supersede(&mut (), next.id)).expect("supersede marks");
        assert_eq!(superseded.state, RevisionState::Superseded);
        let missing = Uuid::new_v4();
        assert!(block_on(repo.copy_forward(&mut (), missing)).is_err());
        assert!(block_on(repo.set_message(&mut (), missing, "x")).is_err());
        assert!(block_on(repo.freeze(&mut (), missing, &[])).is_err());
        assert!(block_on(repo.publish(&mut (), missing)).is_err());
        assert!(block_on(repo.supersede(&mut (), missing)).is_err());
    }

    #[test]
    fn test_fake_works_round_trip_every_method() {
        use marginalia_types::works::WorkDraft;
        let repo = FakeWorks {
            work: Mutex::new(None),
            fail_slug: false,
        };
        assert!(block_on(repo.get_by_slug("essay")).expect("get").is_none());
        assert!(block_on(repo.list()).expect("list").is_empty());
        let work = block_on(repo.insert(
            &mut (),
            WorkDraft {
                slug: "essay".to_owned(),
                title: "Essay".to_owned(),
                work_type: "essay".to_owned(),
                language: None,
                abstract_text: None,
                metadata: Map::new(),
            },
        ))
        .expect("insert stores");
        assert_eq!(
            block_on(repo.get(work.id))
                .expect("get")
                .expect("present")
                .slug,
            "essay"
        );
        assert!(block_on(repo.get(Uuid::new_v4())).expect("get").is_none());
        assert!(block_on(repo.get_by_slug("nope")).expect("get").is_none());
        assert_eq!(block_on(repo.list()).expect("list").len(), 1);
        let revision_id = Uuid::new_v4();
        block_on(repo.set_current_revision(&mut (), work.id, revision_id)).expect("pointer moves");
        assert_eq!(
            block_on(repo.get(work.id))
                .expect("get")
                .expect("present")
                .current_revision_id,
            Some(revision_id)
        );
        assert!(block_on(repo.set_current_revision(&mut (), Uuid::new_v4(), revision_id)).is_err());
        // A stale stamp fails; the live stamp applies fields and re-stamps.
        let live = block_on(repo.get(work.id)).expect("get").expect("present");
        let mut fields = Map::new();
        fields.insert("title".to_owned(), Value::String("New".to_owned()));
        fields.insert("note".to_owned(), Value::String("kept".to_owned()));
        fields.insert("work_type".to_owned(), Value::String("dossier".to_owned()));
        let renamed = block_on(repo.update(&mut (), work.id, live.updated_at, fields))
            .expect("update applies");
        assert_eq!(renamed.title, "New");
        assert_eq!(renamed.work_type, "dossier");
        assert_eq!(
            renamed.metadata.get("note"),
            Some(&Value::String("kept".to_owned()))
        );
        let stale = live.updated_at + chrono::Duration::seconds(3600);
        assert!(block_on(repo.update(&mut (), work.id, stale, Map::new())).is_err());
        assert!(
            block_on(repo.update(&mut (), Uuid::new_v4(), live.updated_at, Map::new())).is_err()
        );
        let archived = block_on(repo.archive(&mut (), work.id)).expect("archive marks");
        assert_eq!(archived.status, WorkStatus::Archived);
        assert!(block_on(repo.archive(&mut (), Uuid::new_v4())).is_err());
    }

    #[test]
    fn test_fake_blocks_round_trip_every_method() {
        let repo = FakeBlocks {
            tree: Mutex::new(vec![]),
            fail_tree: false,
        };
        let revision_id = block_uuid(0x4444_4444_4444_4444_4444_4444_4444_4444);
        let key = block_uuid(0xffff_ffff_ffff_ffff_ffff_ffff_ffff_ffff);
        let draft = |body: &str| marginalia_types::works::WorkBlockDraft {
            revision_id,
            block_key: key,
            parent_id: None,
            position: 0,
            block_type: "paragraph".to_owned(),
            title: None,
            body_markdown: body.to_owned(),
            attributes: Map::new(),
        };
        let row =
            block_on(repo.upsert(&mut (), revision_id, draft("first"), None)).expect("insert");
        assert_eq!(row.body_markdown, "first");
        assert_eq!(block_on(repo.tree(revision_id)).expect("tree").len(), 1);
        // The fake serves one revision: any id lists the stored rows.
        assert_eq!(block_on(repo.tree(Uuid::new_v4())).expect("tree").len(), 1);
        assert_eq!(
            block_on(repo.get(row.id))
                .expect("get")
                .expect("present")
                .id,
            row.id
        );
        assert!(block_on(repo.get(Uuid::new_v4())).expect("get").is_none());
        assert!(block_on(repo.by_key(revision_id, key))
            .expect("by key")
            .is_some());
        assert!(block_on(repo.by_key(Uuid::new_v4(), key))
            .expect("by key")
            .is_none());
        assert!(block_on(repo.by_key_in_tx(&mut (), revision_id, key))
            .expect("by key in tx")
            .is_some());
        // A stale stamp fails; the live stamp rewrites the row in place.
        let live = block_on(repo.get(row.id)).expect("get").expect("present");
        let stale = live.updated_at + chrono::Duration::seconds(3600);
        assert!(block_on(repo.upsert(&mut (), revision_id, draft("stale"), Some(stale))).is_err());
        let rewritten =
            block_on(repo.upsert(&mut (), revision_id, draft("second"), Some(live.updated_at)))
                .expect("rewrite");
        assert_eq!(rewritten.id, row.id);
        assert_eq!(rewritten.body_markdown, "second");
        block_on(repo.delete(&mut (), row.id)).expect("delete removes");
        assert!(block_on(repo.get(row.id)).expect("get").is_none());
        assert!(block_on(repo.tree(revision_id)).expect("tree").is_empty());
    }

    #[test]
    fn test_fake_cites_round_trip_every_method() {
        use marginalia_types::citations::{CitationItemDraft, OccurrenceDraft};
        let repo = FakeCites {
            entries: Mutex::new(vec![]),
        };
        let block_id = block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb);
        let cite_key = block_uuid(0xaaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa_aaaa);
        let occurrence = block_on(repo.insert_occurrence(
            &mut (),
            OccurrenceDraft {
                block_id,
                citation_key: cite_key,
                placement: Placement::Inline,
                intent: Intent::Support,
                note: None,
            },
        ))
        .expect("occurrence inserts");
        let span_id = block_uuid(0xcccc_cccc_cccc_cccc_cccc_cccc_cccc_cccc);
        let item = block_on(repo.insert_item(
            &mut (),
            CitationItemDraft {
                occurrence_id: occurrence.id,
                position: 0,
                edition_id: None,
                edition_key: Some("ED1".to_owned()),
                source_span_id: Some(span_id),
                quoted_text: Some("words".to_owned()),
                verify_status: Some("exact".to_owned()),
                locator: Map::new(),
                prefix: None,
                suffix: None,
                suppress_author: false,
            },
        ))
        .expect("item inserts");
        assert_eq!(item.quoted_text.as_deref(), Some("words"));
        assert!(block_on(repo.insert_item(
            &mut (),
            CitationItemDraft {
                occurrence_id: Uuid::new_v4(),
                position: 0,
                edition_id: None,
                edition_key: None,
                source_span_id: None,
                quoted_text: None,
                verify_status: None,
                locator: Map::new(),
                prefix: None,
                suffix: None,
                suppress_author: false,
            },
        ))
        .is_err());
        assert_eq!(
            block_on(repo.for_block(block_id)).expect("for block").len(),
            1
        );
        assert!(block_on(repo.for_block(Uuid::new_v4()))
            .expect("for block")
            .is_empty());
        assert_eq!(
            block_on(repo.for_revision(Uuid::new_v4()))
                .expect("for revision")
                .len(),
            1
        );
        assert!(block_on(repo.by_key(Uuid::new_v4(), cite_key))
            .expect("by key")
            .is_some());
        assert!(block_on(repo.by_key(Uuid::new_v4(), Uuid::new_v4()))
            .expect("by key")
            .is_none());
        assert_eq!(
            block_on(repo.citing_span(span_id)).expect("citing").len(),
            1
        );
        assert!(block_on(repo.citing_span(Uuid::new_v4()))
            .expect("citing")
            .is_empty());
        assert_eq!(block_on(repo.citing_key("ED1")).expect("citing").len(), 1);
        assert!(block_on(repo.citing_key("NOPE"))
            .expect("citing")
            .is_empty());
    }

    #[test]
    fn test_fake_links_round_trip_every_method() {
        use marginalia_types::works::{BlockEntityLinkDraft, BlockSourceLinkDraft};
        let repo = FakeLinks {
            sources: Mutex::new(Vec::new()),
            entities: Mutex::new(Vec::new()),
        };
        let block_id = block_uuid(0xbbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb_bbbb);
        let span_id = block_uuid(0xcccc_cccc_cccc_cccc_cccc_cccc_cccc_cccc);
        let entity_id = block_uuid(0xeeee_eeee_eeee_eeee_eeee_eeee_eeee_eeee);
        assert!(block_on(repo.for_block(block_id))
            .expect("for block")
            .sources
            .is_empty());
        block_on(repo.add_source_link(
            &mut (),
            BlockSourceLinkDraft {
                block_id,
                source_span_id: span_id,
                relation: "cites".to_owned(),
                confidence: Some(1.0),
                note: None,
            },
        ))
        .expect("source link adds");
        block_on(repo.add_entity_link(
            &mut (),
            BlockEntityLinkDraft {
                block_id,
                entity_id,
                relation: "about".to_owned(),
                surface_form: Some("dabaris".to_owned()),
            },
        ))
        .expect("entity link adds");
        let links = block_on(repo.for_block(block_id)).expect("for block");
        assert_eq!(links.sources.len(), 1);
        assert_eq!(links.entities.len(), 1);
        assert!(block_on(repo.for_block(Uuid::new_v4()))
            .expect("for block")
            .sources
            .is_empty());
        assert_eq!(block_on(repo.for_span(span_id)).expect("for span").len(), 1);
        assert!(block_on(repo.for_span(Uuid::new_v4()))
            .expect("for span")
            .is_empty());
        assert_eq!(
            block_on(repo.for_entity(entity_id, "about"))
                .expect("for entity")
                .len(),
            1
        );
        assert!(block_on(repo.for_entity(entity_id, "other"))
            .expect("for entity")
            .is_empty());
        assert!(block_on(repo.for_entity(Uuid::new_v4(), "about"))
            .expect("for entity")
            .is_empty());
    }

    #[test]
    fn test_fake_spans_round_trip_every_method() {
        let repo = FakeSpans {
            spans: Mutex::new(HashMap::new()),
        };
        let span = block_on(repo.resolve(&mut (), doc_id(), 10, 60)).expect("resolve stores");
        assert_eq!((span.char_start, span.char_end), (10, 60));
        assert_eq!(
            block_on(repo.get(span.id))
                .expect("get")
                .expect("present")
                .id,
            span.id
        );
        assert!(block_on(repo.get(Uuid::new_v4())).expect("get").is_none());
        assert_eq!(
            block_on(repo.for_document(doc_id()))
                .expect("for document")
                .len(),
            1
        );
        assert!(block_on(repo.for_document(Uuid::new_v4()))
            .expect("for document")
            .is_empty());
        // A versioned span is fresh; the resolved one (no version) is stale.
        let fresh_id = block_uuid(0xcccc_cccc_cccc_cccc_cccc_cccc_cccc_cccc);
        repo.spans
            .lock()
            .expect("fake lock")
            .insert(fresh_id, make_span(fresh_id, 0, 5, Some("pv2")));
        let stale = block_on(repo.stale(10)).expect("stale lists");
        assert_eq!(
            stale.iter().map(|row| row.id).collect::<Vec<_>>(),
            vec![span.id]
        );
        assert!(block_on(repo.stale(0)).expect("stale").is_empty());
    }

    #[test]
    fn test_fake_waivers_round_trip_every_method() {
        use marginalia_types::works::WaiverDraft;
        let repo = FakeWaivers {
            rows: Mutex::new(vec![]),
            fail: false,
        };
        let revision_id = block_uuid(0x4444_4444_4444_4444_4444_4444_4444_4444);
        assert!(block_on(repo.for_revision(revision_id))
            .expect("for revision")
            .is_empty());
        let waiver = block_on(repo.insert(
            &mut (),
            WaiverDraft {
                revision_id,
                rule_id: "AUTH_SPAN_REGION".to_owned(),
                subject: Some("c1".to_owned()),
                actor: "user".to_owned(),
                reason: "earned".to_owned(),
            },
        ))
        .expect("waiver inserts");
        assert_eq!(waiver.subject.as_deref(), Some("c1"));
        assert_eq!(
            block_on(repo.for_revision(revision_id))
                .expect("for revision")
                .len(),
            1
        );
        assert!(block_on(repo.for_revision(Uuid::new_v4()))
            .expect("for revision")
            .is_empty());
    }
}
