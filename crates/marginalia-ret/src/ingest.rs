//! Ingestion orchestration pure logic: chunker registry, structural-demotion
//! reports, structure rebuilds, reindex orphans and identity checks,
//! batch-halving depth, backfill routes and reports, and orchestrator
//! decisions (language resolution, ingest stats, content-hash dedup).
//!
//! Python sources:
//! `services/ingestion/{pipeline,structure,reindex,embed_batches,
//! text_backfill,embedding_backfill,orchestrator}.py`.
//!
//! This module executes nothing: no chunker runs here, no SQL runs here, no
//! filesystem or embedding backend is touched. Chunking itself lives in
//! `marginalia-chunk`, section splitting and date scanning in
//! `marginalia-text`, node-tree building in `marginalia-works`; this module
//! only wires those crates' contracts (chunker ids and versions, span
//! containment, report shapes). Database writes stay in the `repos` pass,
//! which executes the SQL shapes built here via `sqlx`.
//!
//! The `CORE_CHUNKERS` versions below mirror the `VERSION` constants in
//! `marginalia-chunk` (`fixed_window` 3.0, the rest 4.0); if those ever move,
//! this table moves with them.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use uuid::Uuid;

// ---------------------------------------------------------------------------
// Chunker registry (`pipeline.py`)
// ---------------------------------------------------------------------------

/// `(chunker id, emitted version)` for every core chunker, in registry order.
///
/// Mirrors `CORE_CHUNKERS` plus each class's `version`: only non-Latin text
/// moved in 4.0, so `fixed_window` still emits 3.0.
pub const CORE_CHUNKERS: [(&str, &str); 4] = [
    ("prose_window", "4.0"),
    ("structural", "4.0"),
    ("whole_or_paragraph", "4.0"),
    ("fixed_window", "3.0"),
];

/// Chunker id to the version currently emitted, core plus plugin.
///
/// `plugins` is the plugin registry's contribution (`chunker id` to
/// `version`), injected as a parameter rather than read from a global so the
/// pure decision stays unit-testable. Plugin entries win on key clash, exactly
/// as the Python merge assigns over the core dict. The result is sorted by id,
/// never in registration order.
pub fn current_chunker_versions(plugins: &BTreeMap<String, String>) -> BTreeMap<String, String> {
    let mut versions: BTreeMap<String, String> = CORE_CHUNKERS
        .iter()
        .map(|(id, version)| ((*id).to_owned(), (*version).to_owned()))
        .collect();
    for (id, version) in plugins {
        versions.insert(id.clone(), version.clone());
    }
    versions
}

/// Resolve one chunker's emitted version, core first then plugin.
///
/// Returns `None` for an unknown id; the message for that case is built by
/// [`unknown_chunker_message`].
pub fn chunker_version<'a>(
    chunker_id: &str,
    plugins: &'a BTreeMap<String, String>,
) -> Option<&'a str> {
    if let Some(version) = plugins.get(chunker_id) {
        return Some(version);
    }
    CORE_CHUNKERS
        .iter()
        .find(|(id, _)| *id == chunker_id)
        .map(|(_, version)| *version)
}

/// Error text for an unresolvable chunker id, byte-identical to the Python
/// `ValueError`.
pub fn unknown_chunker_message(chunker_id: &str) -> String {
    format!("Unknown chunker: {chunker_id}")
}

// ---------------------------------------------------------------------------
// Structural demotion (`pipeline.py`: `_reported_structure`, `_report_demotion`)
// ---------------------------------------------------------------------------

/// Counts a parser reports about its own structure. Non-zero here with an
/// empty section table means the structure was found and then dropped.
pub const STRUCTURE_COUNT_KEYS: [&str; 4] = [
    "heading_count",
    "section_count",
    "chapter_count",
    "div_count",
];

/// The fallback chunker when structural chunking finds no section table.
pub const DEMOTION_CHUNKER: &str = "prose_window";

/// The chunker id whose missing section table triggers a demotion report.
pub const STRUCTURAL_CHUNKER: &str = "structural";

/// Warning event: the parser counted structure and then dropped it.
pub const EVENT_SECTIONS_MISSING: &str = "structural_sections_missing";

/// Info event: the document genuinely has no headings.
pub const EVENT_SECTIONS_ABSENT: &str = "structural_sections_absent";

/// Warning detail, byte-identical to the Python log field.
pub const DETAIL_SECTIONS_MISSING: &str = "The parser counted structure and then supplied no metadata['sections'], so it was chunked into prose windows. Its headings are not addressable and its node tree will be a bare root. The parser needs to emit a section table.";

/// Info detail, byte-identical to the Python log field.
pub const DETAIL_SECTIONS_ABSENT: &str = "No sections to chunk on, so prose windows were used. Expected for a document with no headings; if this format always has them, the parser is not reporting them.";

/// Structure the parser says it found, whether or not it handed any over.
///
/// Only positive integers count, mirroring `isinstance(value, int) and
/// value > 0` (which also admits `True`, so a JSON `true` counts as 1;
/// floats never count). Sorted by key, never in metadata order.
pub fn reported_structure(
    metadata: &serde_json::Map<String, serde_json::Value>,
) -> BTreeMap<String, i64> {
    let mut reported = BTreeMap::new();
    for key in STRUCTURE_COUNT_KEYS {
        let count = match metadata.get(key) {
            Some(serde_json::Value::Number(n)) => n.as_i64(),
            Some(serde_json::Value::Bool(true)) => Some(1),
            _ => None,
        };
        if let Some(n) = count {
            if n > 0 {
                reported.insert(key.to_owned(), n);
            }
        }
    }
    reported
}

/// Severity of a demotion announcement.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DemotionLevel {
    /// The parser counted structure and dropped it: a defect.
    Warning,
    /// The document really has no headings: expected.
    Info,
}

/// One demotion announcement: what `run_chunking` logs when structural
/// chunking falls back to prose windows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Demotion {
    pub level: DemotionLevel,
    pub event: &'static str,
    pub parser: Option<String>,
    pub file: Option<String>,
    pub chunker: &'static str,
    pub reported: BTreeMap<String, i64>,
    pub detail: &'static str,
}

/// Decide whether asking for `chunker_id` announces a demotion.
///
/// Returns `None` when nothing is announced: a non-structural chunker asked
/// for directly is not a demotion, and a section table that exists is chunked
/// on silently. Otherwise mirrors `_report_demotion`: non-zero reported
/// counts warn, anything else informs.
pub fn demotion_for(
    chunker_id: &str,
    metadata: &serde_json::Map<String, serde_json::Value>,
    has_sections: bool,
    parser_id: Option<&str>,
) -> Option<Demotion> {
    if chunker_id != STRUCTURAL_CHUNKER || has_sections {
        return None;
    }
    let reported = reported_structure(metadata);
    let file = metadata
        .get("file_name")
        .and_then(serde_json::Value::as_str)
        .map(str::to_owned);
    if reported.is_empty() {
        Some(Demotion {
            level: DemotionLevel::Info,
            event: EVENT_SECTIONS_ABSENT,
            parser: parser_id.map(str::to_owned),
            file,
            chunker: DEMOTION_CHUNKER,
            reported,
            detail: DETAIL_SECTIONS_ABSENT,
        })
    } else {
        Some(Demotion {
            level: DemotionLevel::Warning,
            event: EVENT_SECTIONS_MISSING,
            parser: parser_id.map(str::to_owned),
            file,
            chunker: DEMOTION_CHUNKER,
            reported,
            detail: DETAIL_SECTIONS_MISSING,
        })
    }
}

// ---------------------------------------------------------------------------
// Structure rebuild (`structure.py`)
// ---------------------------------------------------------------------------

/// How far into a section to look for its date. Measured over 728 letters:
/// half the datelines start within 51 characters and nine in ten within 105.
/// Reading further starts finding dates mentioned in the body, which are not
/// the section's own date and are worse than none.
pub const DATELINE_WINDOW: usize = 200;

/// Outcome of rebuilding document structure trees without touching passage
/// text or embeddings.
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct StructureReport {
    pub documents_total: u64,
    pub documents_rebuilt: u64,
    pub documents_without_text: Vec<String>,
    pub nodes_written: u64,
    pub nodes_dated: u64,
    pub passages_repointed: u64,
    pub dry_run: bool,
    pub failures: BTreeMap<String, String>,
}

/// A written structure node, the fields `_repoint_passages` reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TreeNode {
    pub id: Uuid,
    pub char_start: i64,
    pub char_end: i64,
    pub depth: i64,
}

/// A stored passage, the fields `_repoint_passages` reads. Offsets are
/// `None` exactly when the row's columns are NULL; such rows are skipped, as
/// the repoint query only selects rows with a non-null `char_start`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TextPassage {
    pub id: Uuid,
    pub char_start: Option<i64>,
    pub char_end: Option<i64>,
}

/// Index of the innermost node whose span encloses `[char_start, char_end)`.
///
/// Deepest, because every span is enclosed by the root and only the most
/// specific answer is useful: the section a passage sits in, not the document
/// it belongs to. A span straddling two chapters legitimately resolves to
/// their common ancestor — it is contained by nothing narrower. Ties keep the
/// first node, mirroring the strict `>` comparison.
pub fn deepest_containing_index(
    nodes: &[TreeNode],
    char_start: i64,
    char_end: i64,
) -> Option<usize> {
    let mut best: Option<usize> = None;
    for (index, node) in nodes.iter().enumerate() {
        if node.char_start <= char_start && char_end <= node.char_end {
            let deeper = match best {
                None => true,
                Some(b) => node.depth > nodes[b].depth,
            };
            if deeper {
                best = Some(index);
            }
        }
    }
    best
}

/// Point existing passages at a rebuilt tree, by span containment.
///
/// Returns `(passage id, node id)` updates: passage text and embeddings are
/// untouched, only `node_id` moves. Passages with NULL offsets match nothing.
pub fn repoint_passages(nodes: &[TreeNode], passages: &[TextPassage]) -> Vec<(Uuid, Uuid)> {
    if nodes.is_empty() {
        return Vec::new();
    }
    let mut updates = Vec::new();
    for passage in passages {
        let (Some(start), Some(end)) = (passage.char_start, passage.char_end) else {
            continue;
        };
        if let Some(index) = deepest_containing_index(nodes, start, end) {
            updates.push((passage.id, nodes[index].id));
        }
    }
    updates
}

/// Attach draft spans to their deepest containing node, in memory.
///
/// The `ingest_drafts` analogue of [`repoint_passages`] for passages whose
/// ids do not exist yet: node ids only exist once the tree is written, so a
/// chunker cannot do this itself. Resolving here rather than with a range
/// query per passage matters — a book is thousands of passages over a few
/// hundred nodes. `None` keeps the draft's lack of a node: structure stays
/// optional.
pub fn attach_node_ids(nodes: &[TreeNode], spans: &[(i64, i64)]) -> Vec<Option<Uuid>> {
    spans
        .iter()
        .map(|(start, end)| deepest_containing_index(nodes, *start, *end).map(|i| nodes[i].id))
        .collect()
}

/// Rows `_repoint_passages` reads: id plus offsets for one document's
/// passages with a non-null start.
pub const REPOINT_SELECT_SQL: &str = "SELECT id, char_start, char_end FROM core.passages WHERE document_id = $1 AND char_start IS NOT NULL";

/// One repoint write, executed per `(passage id, node id)` update.
pub const REPOINT_UPDATE_SQL: &str = "UPDATE core.passages SET node_id = $1 WHERE id = $2";

/// Candidate documents for a structure rebuild: every document with
/// canonical text, optionally narrowed to `document_ids` and to documents
/// with no nodes yet. Mirrors `StructureService._candidates`, including the
/// `ORDER BY d.id` that keeps dry runs comparable.
pub fn structure_candidates_sql(
    document_ids: Option<&[Uuid]>,
    only_missing: bool,
) -> (String, Vec<SqlParam>) {
    let mut sql = String::from(
        "SELECT d.id, d.title FROM core.documents d \
         JOIN core.document_texts t ON t.document_id = d.id",
    );
    let mut params = Vec::new();
    let mut clauses = Vec::new();
    if let Some(ids) = document_ids {
        params.push(SqlParam::UuidList(ids.to_vec()));
        clauses.push(format!("d.id = ANY(${})", params.len()));
    }
    if only_missing {
        clauses.push(
            "NOT EXISTS (SELECT 1 FROM core.document_nodes n \
             WHERE n.document_id = d.id)"
                .to_owned(),
        );
    }
    if !clauses.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&clauses.join(" AND "));
    }
    sql.push_str(" ORDER BY d.id");
    (sql, params)
}

// ---------------------------------------------------------------------------
// SQL plumbing (no `sqlx` in this pure module)
// ---------------------------------------------------------------------------

/// Ordered parameter values for the SQL builders here. The `repos` pass binds
/// these positionally to `$N` placeholders.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SqlParam {
    Uuid(Uuid),
    UuidList(Vec<Uuid>),
    Text(String),
    Int(i64),
}

// ---------------------------------------------------------------------------
// Reindex (`reindex.py`)
// ---------------------------------------------------------------------------

/// Fail the run above this fraction of unmatched passages. A silent 3% loss
/// of extractions is exactly the failure this phase exists to prevent.
pub const DEFAULT_ORPHAN_THRESHOLD: f64 = 0.005;

/// `(table, column)` pairs that reference a passage and carry research
/// content. `passage_embeddings` and `passage_fts` are deliberately absent:
/// they are derived from passage text and are regenerated, so cascading them
/// away with the old rows is correct.
pub const DEPENDENTS: [(&str, &str); 5] = [
    ("mentions", "passage_id"),
    ("extractions", "passage_id"),
    ("extraction_records", "passage_id"),
    ("events", "source_passage_id"),
    ("edges", "source_passage_id"),
];

/// Count query for one dependent of one passage. The `repos` pass binds the
/// passage id to `$1`.
pub fn dependent_count_sql(table: &str, column: &str) -> String {
    format!("SELECT COUNT(*) FROM core.{table} WHERE {column} = $1")
}

/// An old passage whose text could not be located in the canonical text.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Orphan {
    pub document_id: Uuid,
    pub passage_id: Uuid,
    pub text_preview: String,
    /// Dependent rows per table, keyed by table name.
    pub dependents: BTreeMap<String, i64>,
    pub reason: String,
}

impl Orphan {
    /// Every research row stranded by this orphan, across all tables.
    pub fn dependent_total(&self) -> i64 {
        self.dependents.values().sum()
    }
}

/// Outcome of re-chunking documents onto current chunker versions.
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct ReindexReport {
    pub dry_run: bool,
    pub documents_total: u64,
    pub documents_reindexed: u64,
    pub documents_up_to_date: u64,
    /// Documents a current chunker reproduces byte-identically, carried onto
    /// the new version by updating the label rather than by re-embedding.
    pub documents_relabelled: u64,
    pub passages_relabelled: u64,
    /// Structure nodes written while re-chunking. Zero across a whole run
    /// means the outline tools will still return nothing.
    pub nodes_written: u64,
    /// Of those, the ones that state their own date. Reported because a
    /// re-chunk that silently drops the dates looks identical to one that
    /// keeps them — the node count is the same either way.
    pub nodes_dated: u64,
    pub documents_without_text: Vec<Uuid>,
    /// Structurally chunked documents, which cannot be re-chunked from
    /// canonical text alone — their section decomposition lives on the
    /// document, not here.
    pub documents_needing_reingest: Vec<Uuid>,
    pub documents_failed: BTreeMap<String, String>,
    pub passages_before: u64,
    pub passages_after: u64,
    pub repointed: BTreeMap<String, i64>,
    pub orphans: Vec<Orphan>,
    pub collisions: u64,
    /// True when the preflight pass refused to let the real pass run.
    pub aborted: bool,
}

impl ReindexReport {
    /// Fraction of pre-existing passages left unmatched. Zero when nothing
    /// was there to match, so an empty corpus does not fail itself.
    pub fn orphan_rate(&self) -> f64 {
        if self.passages_before == 0 {
            0.0
        } else {
            self.orphans.len() as f64 / self.passages_before as f64
        }
    }

    /// Research rows stranded by every orphan.
    pub fn orphaned_dependents(&self) -> i64 {
        self.orphans.iter().map(Orphan::dependent_total).sum()
    }

    /// True when the orphan rate exceeds `threshold` and the run must fail.
    /// Strictly greater: a rate exactly on the threshold still passes.
    pub fn should_fail(&self, threshold: f64) -> bool {
        self.orphan_rate() > threshold
    }
}

/// The span-and-text identity `_output_is_identical` compares.
pub trait PassageIdentity {
    fn char_start(&self) -> i64;
    fn char_end(&self) -> i64;
    fn text(&self) -> &str;
}

/// True when re-chunking would reproduce exactly the passages already stored.
///
/// Compares spans and text, which is the whole of what a passage *is* for
/// retrieval and citation: the same characters at the same offsets embed to
/// the same vector and verify the same quote. Version, token estimate and
/// metadata are deliberately excluded — they are labels on identical content,
/// and treating a changed label as changed content is what would force a
/// corpus-wide re-embed.
pub fn output_is_identical<O: PassageIdentity, N: PassageIdentity>(old: &[O], new: &[N]) -> bool {
    if old.len() != new.len() {
        return false;
    }
    old.iter().zip(new.iter()).all(|(o, n)| {
        o.char_start() == n.char_start() && o.char_end() == n.char_end() && o.text() == n.text()
    })
}

// ---------------------------------------------------------------------------
// Embedding batches (`embed_batches.py`)
// ---------------------------------------------------------------------------

/// 32 -> 16 -> 8 -> 4 -> 2 -> 1 is five halvings. Below one passage there is
/// nothing left to split, and the failure is real rather than memory pressure.
pub const MAX_HALVING_DEPTH: u32 = 5;

/// Batch size halving starts from: the first retry halves 32 into 16 + 16.
pub const INITIAL_BATCH_SIZE: usize = 32;

/// Batch size to retry at after a failure at `depth`, or `None` when the
/// ladder is exhausted. Depth 0 is the first failure (of 32), so depth 0
/// yields 16, depth 4 yields 1, and anything past depth 5 yields nothing.
pub fn next_batch_on_failure(depth: u32) -> Option<usize> {
    const LADDER: [usize; 5] = [16, 8, 4, 2, 1];
    LADDER.get(depth as usize).copied()
}

/// Split a failing batch the way `embed_and_store` does: `mid = len // 2`,
/// left keeps `mid`, right keeps the rest.
pub fn split_batch(len: usize) -> (usize, usize) {
    let mid = len / 2;
    (mid, len - mid)
}

/// Why one embed call failed. Halving answers memory pressure; it cannot
/// answer a backend that is not there, where the same call fails identically
/// at size 1 and each retry costs a full timeout while buying nothing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EmbedFailure {
    /// The backend is unreachable: abort the run, do not halve.
    Unavailable,
    /// Anything else (memory pressure, a bad row): halve and retry.
    Retriable,
}

/// Whether a failed batch of `batch_len` at `depth` halves or is recorded as
/// failed passages. Mirrors the `except` branch: a single passage and a depth
/// past [`MAX_HALVING_DEPTH`] are terminal, as is an unavailable backend.
pub fn should_halve(failure: EmbedFailure, batch_len: usize, depth: u32) -> bool {
    match failure {
        EmbedFailure::Unavailable => false,
        EmbedFailure::Retriable => batch_len > 1 && depth < MAX_HALVING_DEPTH,
    }
}

/// How one embed batch (after any halving) came out. Shared by the backfill
/// and the re-chunk, because a batch that fails in one fails in the other for
/// the same reason.
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct BatchOutcome {
    pub embedded: u64,
    pub failed_batches: u64,
    pub halvings: u64,
    pub failed_passages: Vec<Uuid>,
}

// ---------------------------------------------------------------------------
// Text backfill (`text_backfill.py`)
// ---------------------------------------------------------------------------

/// Modules heavy enough that a backfill run over them should be a deliberate
/// act.
pub const SLOW_MODULES: [&str; 1] = ["docling"];

/// True for a module id whose parse costs minutes to hours per large scan.
pub fn is_slow_module(module_id: &str) -> bool {
    SLOW_MODULES.contains(&module_id)
}

/// Recovery tier for a document lacking canonical text. Fast (seconds) and
/// slow (minutes to hours) cost wildly different amounts; unreachable needs
/// the pack that fetched it; a missing file is gone; already-present needs no
/// work.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Route {
    Fast,
    Slow,
    Unreachable,
    MissingFile,
    AlreadyPresent,
}

/// A source that is not a filesystem path can only be re-fetched by the pack
/// that produced it. Mirrors the pack-URI check: `://` anywhere, or a `:` in
/// a path that does not start at the root.
pub fn is_pack_uri(source: &str) -> bool {
    source.contains("://") || (source.contains(':') && !source.starts_with('/'))
}

/// Detail for a pack-URI source, byte-identical to the Python.
pub const DETAIL_PACK_URI: &str = "source is a pack URI; re-run that pack's ingest";

/// Detail for a source file that no longer exists, byte-identical.
pub const DETAIL_MISSING_FILE: &str = "source file no longer exists";

/// Detail for a source no ingestion module accepts, byte-identical up to the
/// trailing cause.
pub fn dispatch_failure_detail(exc: &str) -> String {
    format!("no ingestion module accepts this source: {exc}")
}

/// The classification of one document: its route, the accepting module when
/// one exists, and why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Classification {
    pub route: Route,
    pub module_id: Option<String>,
    pub detail: String,
}

/// Classify one document lacking canonical text by recovery route.
///
/// `file_exists` and `dispatch` stand in for the filesystem and dispatcher
/// probes, which stay outside this pure module: the caller only consults the
/// dispatcher when the file exists, mirroring the order of checks. `dispatch`
/// is `Ok(module id)` when a module accepts the source and `Err(cause)` when
/// none does.
pub fn classify_route(
    source: &str,
    file_exists: bool,
    dispatch: Result<&str, &str>,
) -> Classification {
    if is_pack_uri(source) {
        return Classification {
            route: Route::Unreachable,
            module_id: None,
            detail: DETAIL_PACK_URI.to_owned(),
        };
    }
    if !file_exists {
        return Classification {
            route: Route::MissingFile,
            module_id: None,
            detail: DETAIL_MISSING_FILE.to_owned(),
        };
    }
    match dispatch {
        Err(exc) => Classification {
            route: Route::Unreachable,
            module_id: None,
            detail: dispatch_failure_detail(exc),
        },
        Ok(module_id) => Classification {
            route: if is_slow_module(module_id) {
                Route::Slow
            } else {
                Route::Fast
            },
            module_id: Some(module_id.to_owned()),
            detail: String::new(),
        },
    }
}

/// A document lacking canonical text, with its recovery route.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Candidate {
    pub document_id: Uuid,
    pub title: Option<String>,
    pub source: String,
    pub document_type: String,
    pub parser: String,
    pub route: Route,
    pub module_id: Option<String>,
    pub size_bytes: Option<u64>,
    pub detail: String,
}

/// Outcome of recovering canonical text by re-parsing reachable sources.
///
/// Recovery is deliberately *not* re-anchoring: this stores the substrate and
/// `reindex chunks` then re-anchors, whose orphan report tells whether the
/// recovered text actually matches what the old passages were cut from.
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct TextBackfillReport {
    pub dry_run: bool,
    pub candidates: Vec<Candidate>,
    pub recovered: u64,
    pub failed: BTreeMap<String, String>,
}

impl TextBackfillReport {
    /// Candidates grouped by route. Sorted by route, never in plan order.
    pub fn grouped(&self) -> BTreeMap<Route, Vec<&Candidate>> {
        let mut grouped: BTreeMap<Route, Vec<&Candidate>> = BTreeMap::new();
        for candidate in &self.candidates {
            grouped.entry(candidate.route).or_default().push(candidate);
        }
        grouped
    }
}

/// Error text when a parser hands back no text, byte-identical to the Python
/// `ValueError`.
pub const RECOVER_EMPTY_TEXT: &str = "parser produced no text";

/// Reject an empty recovery before it is stored: without text the offsets
/// would address nothing.
pub fn validate_recovered_text(full_text: &str) -> Result<&str, &'static str> {
    if full_text.trim().is_empty() {
        Err(RECOVER_EMPTY_TEXT)
    } else {
        Ok(full_text)
    }
}

// ---------------------------------------------------------------------------
// Embedding backfill (`embedding_backfill.py`)
// ---------------------------------------------------------------------------

/// Embedding coverage for the active model.
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct CoverageReport {
    pub model: String,
    pub model_version: String,
    pub dim: u64,
    pub total_passages: u64,
    pub embedded: u64,
    pub missing: u64,
    pub wrong_dimension: u64,
    pub foreign_models: BTreeMap<String, i64>,
}

impl CoverageReport {
    /// True when nothing is missing and nothing is wrongly sized.
    pub fn complete(&self) -> bool {
        self.missing == 0 && self.wrong_dimension == 0
    }

    /// `embedded / total`, where an empty corpus is vacuously fully covered.
    pub fn coverage(&self) -> f64 {
        if self.total_passages == 0 {
            1.0
        } else {
            self.embedded as f64 / self.total_passages as f64
        }
    }
}

/// Outcome of filling embedding gaps, including the recovery path for an
/// interrupted ingest.
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct BackfillReport {
    pub dry_run: bool,
    pub candidates: u64,
    pub embedded: u64,
    pub failed_batches: u64,
    /// Passages that failed even alone — genuinely unembeddable, not memory
    /// pressure artefacts.
    pub failed_passages: Vec<Uuid>,
    /// Times a batch had to be split. A high count means the embedding batch
    /// size is too large for this corpus and hardware.
    pub halvings: u64,
}

// ---------------------------------------------------------------------------
// Orchestrator (`orchestrator.py`)
// ---------------------------------------------------------------------------

/// Prefer what the caller or parser knows; otherwise the configured default.
///
/// Either side may be absent, and an empty `supplied` falls back exactly like
/// a missing one, mirroring `supplied or default`.
pub fn resolve_language<'a>(
    supplied: Option<&'a str>,
    default: Option<&'a str>,
) -> Option<&'a str> {
    match supplied {
        Some(known) if !known.is_empty() => Some(known),
        _ => default,
    }
}

/// Per-run ingest counters, with the exact key set `ingest_paths` returns.
#[derive(Debug, Default, Clone, Copy, Serialize, Deserialize)]
pub struct IngestStats {
    pub total: u64,
    pub ok: u64,
    pub skipped: u64,
    pub failed: u64,
}

impl IngestStats {
    /// The stats keys, in the order `ingest_paths` initialises them.
    pub const KEYS: [&'static str; 4] = ["total", "ok", "skipped", "failed"];

    /// Run status from the counters: clean, mixed, or total failure.
    pub fn status(&self) -> &'static str {
        if self.failed == 0 {
            "ok"
        } else if self.ok > 0 {
            "partial"
        } else {
            "failed"
        }
    }
}

/// The exact bytes hashed for dedup identity: the canonical text when the
/// caller supplies it, otherwise the drafts joined with `\n`.
///
/// Hashing the *content*, not `source:title`: the old form identified a
/// document by its metadata, so re-ingesting the same file under a
/// differently-cased title produced a second document the unique constraint
/// could not catch. Note `Some("")` is used as-is — only `None` falls back.
pub fn dedup_hash_input(full_text: Option<&str>, draft_texts: &[&str]) -> String {
    match full_text {
        Some(text) => text.to_owned(),
        None => draft_texts.join("\n"),
    }
}

/// Marker on the duplicate outcome, byte-identical to the Python.
pub const SKIPPED_DUPLICATE: &str = "duplicate";

/// Whether a `find_by_hash` hit skips the ingest.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DedupDecision {
    /// A document with this content hash and source already exists: reuse its
    /// id and passage count, ingest nothing.
    SkipDuplicate,
    /// Genuinely new content (or a new source): ingest.
    IngestNew,
}

/// A `find_by_hash` hit means skip; anything else ingests.
pub fn dedup_decision(hash_hit: bool) -> DedupDecision {
    if hash_hit {
        DedupDecision::SkipDuplicate
    } else {
        DedupDecision::IngestNew
    }
}

/// A freshly ingested document: its id, passage count, and node count.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct DraftIngestOutcome {
    pub document_id: Uuid,
    pub passage_count: usize,
    pub node_count: usize,
}

/// A skipped duplicate: the existing id and its current passage count.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct DuplicateOutcome {
    pub document_id: Uuid,
    pub passage_count: usize,
}

impl DuplicateOutcome {
    /// The `skipped` marker on the duplicate shape.
    pub fn skipped(&self) -> &'static str {
        SKIPPED_DUPLICATE
    }
}

/// Error text when neither lookup key is given, byte-identical to the Python
/// `ValueError`.
pub const FIND_EXISTING_REQUIRES_ARG: &str = "find_existing requires source or source_pattern";

/// Require at least one of `source` (exact match) or `source_pattern`
/// (case-insensitive substring) before looking up ingested documents.
pub fn validate_find_existing(
    source: Option<&str>,
    source_pattern: Option<&str>,
) -> Result<(), &'static str> {
    if source.is_none() && source_pattern.is_none() {
        return Err(FIND_EXISTING_REQUIRES_ARG);
    }
    Ok(())
}

/// Exact-source matching: only the document stored under this source matches.
pub fn source_matches_exact(doc_source: &str, source: &str) -> bool {
    doc_source == source
}

/// Pattern matching: case-insensitive substring, mirroring the repository's
/// `source_pattern` semantics.
pub fn source_matches_pattern(doc_source: &str, pattern: &str) -> bool {
    doc_source.to_lowercase().contains(&pattern.to_lowercase())
}

// ---------------------------------------------------------------------------
// Tests (mirroring the seven Python unit suites case-for-case)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn metadata(pairs: &[(&str, serde_json::Value)]) -> serde_json::Map<String, serde_json::Value> {
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), v.clone()))
            .collect()
    }

    fn int(n: i64) -> serde_json::Value {
        serde_json::Value::from(n)
    }

    // --- test_chunking_demotion.py ---

    #[test]
    fn counted_structure_then_dropped_warns() {
        let meta = metadata(&[
            ("heading_count", int(39)),
            ("file_name", serde_json::Value::from("a.md")),
        ]);
        let demotion =
            demotion_for("structural", &meta, false, Some("markdown")).expect("must announce");
        assert_eq!(demotion.level, DemotionLevel::Warning);
        assert_eq!(demotion.event, "structural_sections_missing");
        assert_eq!(demotion.parser.as_deref(), Some("markdown"));
        assert_eq!(demotion.file.as_deref(), Some("a.md"));
        assert_eq!(demotion.chunker, "prose_window");
        assert_eq!(
            demotion.reported,
            BTreeMap::from([("heading_count".to_owned(), 39)])
        );
        assert_eq!(demotion.detail, DETAIL_SECTIONS_MISSING);
    }

    #[test]
    fn flat_file_with_zero_counts_informs_without_warning() {
        let meta = metadata(&[
            ("heading_count", int(0)),
            ("file_name", serde_json::Value::from("flat.md")),
        ]);
        let demotion =
            demotion_for("structural", &meta, false, Some("markdown")).expect("must announce");
        assert_eq!(demotion.level, DemotionLevel::Info);
        assert_eq!(demotion.event, "structural_sections_absent");
        assert_eq!(demotion.detail, DETAIL_SECTIONS_ABSENT);
    }

    #[test]
    fn no_metadata_at_all_is_absent_not_broken() {
        let meta = metadata(&[]);
        let demotion = demotion_for("structural", &meta, false, None).expect("must announce");
        assert_eq!(demotion.event, "structural_sections_absent");
        assert_eq!(demotion.level, DemotionLevel::Info);
    }

    #[test]
    fn each_structure_count_key_warns() {
        for key in STRUCTURE_COUNT_KEYS {
            let meta = metadata(&[(key, int(7))]);
            let demotion =
                demotion_for("structural", &meta, false, Some("p")).expect("must announce");
            assert_eq!(demotion.event, EVENT_SECTIONS_MISSING, "key {key}");
            assert_eq!(
                demotion.reported,
                BTreeMap::from([(key.to_owned(), 7)]),
                "key {key}"
            );
        }
    }

    #[test]
    fn document_with_sections_says_nothing() {
        let meta = metadata(&[("heading_count", int(1))]);
        assert_eq!(
            demotion_for("structural", &meta, true, Some("markdown")),
            None
        );
    }

    #[test]
    fn prose_chunker_asked_for_directly_is_not_reported() {
        let meta = metadata(&[("heading_count", int(5))]);
        assert_eq!(demotion_for("prose_window", &meta, false, None), None);
    }

    #[test]
    fn reported_structure_ignores_non_positive_and_non_integers() {
        let meta = metadata(&[
            ("heading_count", int(0)),
            ("section_count", int(-3)),
            ("chapter_count", serde_json::Value::from(2.5)),
            ("div_count", serde_json::Value::from("9")),
        ]);
        assert!(reported_structure(&meta).is_empty());
    }

    #[test]
    fn reported_structure_counts_json_true_as_one() {
        // Python `isinstance(value, int) and value > 0` admits `True`.
        let meta = metadata(&[("heading_count", serde_json::Value::Bool(true))]);
        assert_eq!(
            reported_structure(&meta),
            BTreeMap::from([("heading_count".to_owned(), 1)])
        );
        let meta = metadata(&[("heading_count", serde_json::Value::Bool(false))]);
        assert!(reported_structure(&meta).is_empty());
    }

    // --- test_embed_batches.py (pure ladder; IO halves stay in the services) ---

    #[test]
    fn halving_ladder_runs_32_to_1_then_stops() {
        assert_eq!(
            (0..5).map(next_batch_on_failure).collect::<Vec<_>>(),
            [Some(16), Some(8), Some(4), Some(2), Some(1)]
        );
        assert_eq!(next_batch_on_failure(5), None);
        assert_eq!(next_batch_on_failure(6), None);
        assert_eq!(next_batch_on_failure(u32::MAX), None);
    }

    #[test]
    fn max_halving_depth_is_five() {
        assert_eq!(MAX_HALVING_DEPTH, 5);
    }

    #[test]
    fn unreachable_backend_never_halves() {
        assert!(!should_halve(EmbedFailure::Unavailable, 16, 0));
        assert!(!should_halve(EmbedFailure::Unavailable, 32, 0));
    }

    #[test]
    fn ordinary_failure_halves_until_depth_or_single() {
        assert!(should_halve(EmbedFailure::Retriable, 16, 0));
        assert!(should_halve(EmbedFailure::Retriable, 2, 4));
        assert!(!should_halve(EmbedFailure::Retriable, 1, 0));
        assert!(!should_halve(EmbedFailure::Retriable, 2, 5));
        assert!(!should_halve(EmbedFailure::Retriable, 16, 5));
    }

    #[test]
    fn failing_batch_splits_down_the_middle() {
        assert_eq!(split_batch(16), (8, 8));
        assert_eq!(split_batch(3), (1, 2));
    }

    #[test]
    fn healthy_batch_outcome_starts_empty() {
        let outcome = BatchOutcome::default();
        assert_eq!(outcome.embedded, 0);
        assert_eq!(outcome.failed_batches, 0);
        assert_eq!(outcome.halvings, 0);
        assert!(outcome.failed_passages.is_empty());
    }

    // --- test_reindex_relabel.py ---

    #[derive(Debug)]
    struct Stored {
        start: i64,
        end: i64,
        text: String,
    }

    #[derive(Debug)]
    struct Draft {
        start: i64,
        end: i64,
        text: String,
    }

    impl PassageIdentity for Stored {
        fn char_start(&self) -> i64 {
            self.start
        }
        fn char_end(&self) -> i64 {
            self.end
        }
        fn text(&self) -> &str {
            &self.text
        }
    }

    impl PassageIdentity for Draft {
        fn char_start(&self) -> i64 {
            self.start
        }
        fn char_end(&self) -> i64 {
            self.end
        }
        fn text(&self) -> &str {
            &self.text
        }
    }

    fn stored(start: i64, end: i64, text: &str) -> Stored {
        Stored {
            start,
            end,
            text: text.to_owned(),
        }
    }

    fn draft(start: i64, end: i64, text: &str) -> Draft {
        Draft {
            start,
            end,
            text: text.to_owned(),
        }
    }

    #[test]
    fn identical_spans_and_text_are_unchanged() {
        let old = [stored(0, 10, "the clerk "), stored(10, 20, "wrote it. ")];
        let new = [draft(0, 10, "the clerk "), draft(10, 20, "wrote it. ")];
        assert!(output_is_identical(&old, &new));
    }

    #[test]
    fn changed_boundary_is_not_unchanged() {
        let old = [stored(0, 10, "the clerk "), stored(10, 20, "wrote it. ")];
        let new = [draft(0, 12, "the clerk w"), draft(12, 20, "rote it. ")];
        assert!(!output_is_identical(&old, &new));
    }

    #[test]
    fn different_passage_count_is_not_unchanged() {
        let old = [stored(0, 20, "the clerk wrote it. ")];
        let new = [draft(0, 10, "the clerk "), draft(10, 20, "wrote it. ")];
        assert!(!output_is_identical(&old, &new));
    }

    #[test]
    fn same_span_but_different_text_is_not_unchanged() {
        let old = [stored(0, 10, "the clerk ")];
        let new = [draft(0, 10, "the CLERK ")];
        assert!(!output_is_identical(&old, &new));
    }

    #[test]
    fn labels_alone_do_not_count_as_a_change() {
        // `output_is_identical` only sees spans and text, so a version bump
        // or a re-estimated token count can never surface here by construction.
        let old = [stored(0, 10, "λόγος ἦν ὁ")];
        let new = [draft(0, 10, "λόγος ἦν ὁ")];
        assert!(output_is_identical(&old, &new));
    }

    #[test]
    fn empty_document_compares_equal() {
        let old: [Stored; 0] = [];
        let new: [Draft; 0] = [];
        assert!(output_is_identical(&old, &new));
    }

    // --- test_structure_rebuild.py (pure parts: window, repoint) ---
    // Dating itself lives in `marginalia-text` and the tree in
    // `marginalia-works`; only the wiring here is pinned.

    #[test]
    fn dateline_window_is_200_chars() {
        assert_eq!(DATELINE_WINDOW, 200);
    }

    fn node(id: u128, start: i64, end: i64, depth: i64) -> TreeNode {
        TreeNode {
            id: Uuid::from_u128(id),
            char_start: start,
            char_end: end,
            depth,
        }
    }

    fn passage(id: u128, start: i64, end: i64) -> TextPassage {
        TextPassage {
            id: Uuid::from_u128(id),
            char_start: Some(start),
            char_end: Some(end),
        }
    }
    #[test]
    fn deepest_nesting_wins_the_index() {
        // Three nested containers all hold the span: the walk keeps the
        // deepest, exercising the replace-best arm rather than first-match.
        let nodes = [
            node(1, 0, 100, 0),
            node(2, 0, 100, 1),
            node(3, 10, 90, 2),
            // Contains the span but is shallower than the best: the walk
            // keeps the deeper node, exercising the skip arm.
            node(4, 0, 100, 0),
        ];
        assert_eq!(deepest_containing_index(&nodes, 20, 80), Some(2));
        assert_eq!(deepest_containing_index(&nodes, 200, 210), None);
    }

    #[test]
    fn repoint_prefers_the_deepest_containing_node() {
        let nodes = [
            node(1, 0, 100, 0),  // root: contains everything
            node(2, 0, 50, 1),   // first letter
            node(3, 50, 100, 1), // second letter
        ];
        let passages = [passage(11, 5, 40), passage(12, 60, 90)];
        assert_eq!(
            repoint_passages(&nodes, &passages),
            [
                (Uuid::from_u128(11), Uuid::from_u128(2)),
                (Uuid::from_u128(12), Uuid::from_u128(3)),
            ]
        );
    }

    #[test]
    fn straddling_passage_resolves_to_the_common_ancestor() {
        let nodes = [node(1, 0, 100, 0), node(2, 0, 50, 1), node(3, 50, 100, 1)];
        let passages = [passage(11, 40, 60)];
        assert_eq!(
            repoint_passages(&nodes, &passages),
            [(Uuid::from_u128(11), Uuid::from_u128(1))]
        );
    }

    #[test]
    fn passage_outside_every_node_is_left_alone() {
        let nodes = [node(1, 0, 50, 0)];
        assert!(repoint_passages(&nodes, &[passage(11, 60, 90)]).is_empty());
    }

    #[test]
    fn empty_tree_repoints_nothing() {
        assert!(repoint_passages(&[], &[passage(11, 0, 10)]).is_empty());
    }

    #[test]
    fn null_offsets_are_skipped() {
        let nodes = [node(1, 0, 100, 0)];
        let nulls = [
            TextPassage {
                id: Uuid::from_u128(11),
                char_start: None,
                char_end: None,
            },
            TextPassage {
                id: Uuid::from_u128(12),
                char_start: Some(5),
                char_end: None,
            },
        ];
        assert!(repoint_passages(&nodes, &nulls).is_empty());
    }

    // --- test_ingestion_orchestrator.py ---

    #[test]
    fn language_prefers_supplied_then_default() {
        assert_eq!(resolve_language(Some("de"), Some("en")), Some("de"));
        assert_eq!(resolve_language(Some(""), Some("en")), Some("en"));
        assert_eq!(resolve_language(None, Some("en")), Some("en"));
        assert_eq!(resolve_language(None, None), None);
        assert_eq!(resolve_language(Some("grc"), None), Some("grc"));
    }

    #[test]
    fn stats_status_derives_from_counters() {
        let clean = IngestStats {
            total: 3,
            ok: 2,
            skipped: 1,
            failed: 0,
        };
        assert_eq!(clean.status(), "ok");
        let mixed = IngestStats {
            total: 3,
            ok: 1,
            failed: 2,
            ..IngestStats::default()
        };
        assert_eq!(mixed.status(), "partial");
        let failed = IngestStats {
            total: 2,
            failed: 2,
            ..IngestStats::default()
        };
        assert_eq!(failed.status(), "failed");
        assert_eq!(IngestStats::KEYS, ["total", "ok", "skipped", "failed"]);
    }

    #[test]
    fn find_existing_requires_a_key_with_exact_message() {
        assert_eq!(
            validate_find_existing(None, None),
            Err("find_existing requires source or source_pattern")
        );
        assert_eq!(
            FIND_EXISTING_REQUIRES_ARG,
            "find_existing requires source or source_pattern"
        );
        assert!(validate_find_existing(Some("/a.txt"), None).is_ok());
        assert!(validate_find_existing(None, Some("B003")).is_ok());
    }

    #[test]
    fn exact_source_rejects_substring_hits() {
        assert!(source_matches_exact(
            "/tmp/extracted/B003NX6Z3W.txt",
            "/tmp/extracted/B003NX6Z3W.txt"
        ));
        assert!(!source_matches_exact(
            "/tmp/other/B003NX6Z3W.txt",
            "/tmp/extracted/B003NX6Z3W.txt"
        ));
    }

    #[test]
    fn pattern_matches_case_insensitive_substrings() {
        assert!(source_matches_pattern(
            "/tmp/extracted/B003NX6Z3W.txt",
            "b003nx6z3w"
        ));
        assert!(!source_matches_pattern(
            "/tmp/extracted/OTHER.txt",
            "b003nx6z3w"
        ));
    }

    // --- test_ingest_drafts_dedup.py ---

    #[test]
    fn hash_input_prefers_canonical_text() {
        let text = "The archive holds letters. Each letter carries a date.";
        assert_eq!(dedup_hash_input(Some(text), &["other"]), text);
    }

    #[test]
    fn hash_input_falls_back_to_joined_drafts() {
        assert_eq!(dedup_hash_input(None, &["one", "two"]), "one\ntwo");
    }

    #[test]
    fn hash_hit_skips_otherwise_ingests() {
        assert_eq!(dedup_decision(true), DedupDecision::SkipDuplicate);
        assert_eq!(dedup_decision(false), DedupDecision::IngestNew);
        assert_eq!(
            DuplicateOutcome {
                document_id: Uuid::nil(),
                passage_count: 4,
            }
            .skipped(),
            "duplicate"
        );
    }

    // --- test_ingest_drafts_nodes.py ---

    #[test]
    fn passages_attach_to_their_entry_not_the_root() {
        let entry_a = "logos. Word, speech, account.";
        let entry_b = "pistis. Faith, trust, faithfulness.";
        let b_start = entry_a.len() as i64 + 2;
        let text_len = b_start + entry_b.len() as i64;
        let nodes = [
            node(1, 0, text_len, 0),
            node(2, 0, b_start - 2, 1),
            node(3, b_start, text_len, 1),
        ];
        let landed = attach_node_ids(&nodes, &[(0, b_start - 2), (b_start, text_len)]);
        assert_eq!(landed, [Some(Uuid::from_u128(2)), Some(Uuid::from_u128(3))]);
        assert!(!landed.contains(&Some(Uuid::from_u128(1))));
    }

    #[test]
    fn structure_stays_optional() {
        let landed = attach_node_ids(&[], &[(0, 10)]);
        assert_eq!(landed, [None]);
    }

    // --- routes, orphans, coverage, backfill reports ---

    #[test]
    fn pack_uri_sources_are_unreachable() {
        for source in ["logos:LLS:BLSSDRPCMKRTHLF:batch:b0000", "pack://bucket/key"] {
            let classification = classify_route(source, false, Ok("text"));
            assert_eq!(classification.route, Route::Unreachable);
            assert_eq!(classification.module_id, None);
            assert_eq!(classification.detail, DETAIL_PACK_URI);
        }
    }

    #[test]
    fn gone_file_is_missing_not_unreachable() {
        let classification = classify_route("/gone/book.pdf", false, Err("unused"));
        assert_eq!(classification.route, Route::MissingFile);
        assert_eq!(classification.detail, DETAIL_MISSING_FILE);
    }

    #[test]
    fn rejected_source_reports_the_cause() {
        let classification = classify_route("/x/y.zzz", true, Err("unknown suffix"));
        assert_eq!(classification.route, Route::Unreachable);
        assert_eq!(
            classification.detail,
            "no ingestion module accepts this source: unknown suffix"
        );
    }

    #[test]
    fn docling_is_slow_everything_else_fast() {
        let slow = classify_route("/scan/tome.pdf", true, Ok("docling"));
        assert_eq!(slow.route, Route::Slow);
        assert_eq!(slow.module_id.as_deref(), Some("docling"));
        let fast = classify_route("/text/ch.txt", true, Ok("plaintext"));
        assert_eq!(fast.route, Route::Fast);
        assert!(is_slow_module("docling"));
        assert!(!is_slow_module("plaintext"));
    }

    #[test]
    fn report_groups_candidates_by_route() {
        let candidate = |route| Candidate {
            document_id: Uuid::new_v4(),
            title: None,
            source: String::new(),
            document_type: String::new(),
            parser: String::new(),
            route,
            module_id: None,
            size_bytes: None,
            detail: String::new(),
        };
        let report = TextBackfillReport {
            candidates: vec![
                candidate(Route::Fast),
                candidate(Route::Slow),
                candidate(Route::Fast),
            ],
            ..TextBackfillReport::default()
        };
        let grouped = report.grouped();
        assert_eq!(grouped[&Route::Fast].len(), 2);
        assert_eq!(grouped[&Route::Slow].len(), 1);
        assert!(!grouped.contains_key(&Route::Unreachable));
    }

    #[test]
    fn empty_recovery_is_rejected_with_exact_message() {
        assert_eq!(
            validate_recovered_text("   "),
            Err("parser produced no text")
        );
        assert_eq!(RECOVER_EMPTY_TEXT, "parser produced no text");
        assert_eq!(validate_recovered_text("text"), Ok("text"));
    }

    #[test]
    fn coverage_divides_embedded_by_total() {
        let report = CoverageReport {
            total_passages: 4,
            embedded: 3,
            missing: 1,
            ..CoverageReport::default()
        };
        assert_eq!(report.coverage(), 0.75);
        assert!(!report.complete());
        assert!(CoverageReport::default().complete());
    }

    #[test]
    fn empty_corpus_is_vacuously_fully_covered() {
        assert_eq!(CoverageReport::default().coverage(), 1.0);
    }

    #[test]
    fn orphan_totals_sum_dependents_across_tables() {
        let orphan = Orphan {
            document_id: Uuid::nil(),
            passage_id: Uuid::nil(),
            text_preview: "…".to_owned(),
            dependents: BTreeMap::from([("mentions".to_owned(), 2), ("events".to_owned(), 1)]),
            reason: "no anchor".to_owned(),
        };
        assert_eq!(orphan.dependent_total(), 3);
        let report = ReindexReport {
            passages_before: 100,
            orphans: vec![orphan],
            ..ReindexReport::default()
        };
        assert_eq!(report.orphan_rate(), 0.01);
        assert_eq!(report.orphaned_dependents(), 3);
    }

    #[test]
    fn threshold_boundary_passes_only_above_fails() {
        let report = ReindexReport {
            passages_before: 200,
            orphans: vec![Orphan {
                document_id: Uuid::nil(),
                passage_id: Uuid::nil(),
                text_preview: String::new(),
                dependents: BTreeMap::new(),
                reason: String::new(),
            }],
            ..ReindexReport::default()
        };
        assert_eq!(report.orphan_rate(), DEFAULT_ORPHAN_THRESHOLD);
        assert!(!report.should_fail(DEFAULT_ORPHAN_THRESHOLD));
        assert!(report.should_fail(DEFAULT_ORPHAN_THRESHOLD - f64::EPSILON));
        assert!(!ReindexReport::default().should_fail(DEFAULT_ORPHAN_THRESHOLD));
    }

    #[test]
    fn backfill_report_counts_halvings() {
        let report = BackfillReport {
            halvings: 3,
            ..BackfillReport::default()
        };
        assert_eq!(report.halvings, 3);
        assert!(report.failed_passages.is_empty());
    }

    #[test]
    fn core_chunker_table_matches_emitted_versions() {
        let versions = current_chunker_versions(&BTreeMap::new());
        assert_eq!(
            versions,
            BTreeMap::from([
                ("fixed_window".to_owned(), "3.0".to_owned()),
                ("prose_window".to_owned(), "4.0".to_owned()),
                ("structural".to_owned(), "4.0".to_owned()),
                ("whole_or_paragraph".to_owned(), "4.0".to_owned()),
            ])
        );
        // Plugin entries ride along and win on clash, never via a global.
        let plugins = BTreeMap::from([("tei".to_owned(), "1.2".to_owned())]);
        let merged = current_chunker_versions(&plugins);
        assert_eq!(merged["tei"], "1.2");
        assert_eq!(merged["structural"], "4.0");
        assert_eq!(chunker_version("structural", &plugins), Some("4.0"));
        assert_eq!(chunker_version("tei", &plugins), Some("1.2"));
        assert_eq!(chunker_version("nope", &plugins), None);
        assert_eq!(unknown_chunker_message("nope"), "Unknown chunker: nope");
    }

    #[test]
    fn sql_shapes_use_positional_placeholders() {
        assert!(REPOINT_SELECT_SQL.contains("$1"));
        assert!(REPOINT_UPDATE_SQL.contains("$1"));
        assert!(REPOINT_UPDATE_SQL.contains("$2"));
        let (sql, params) = structure_candidates_sql(None, false);
        assert!(sql.ends_with("ORDER BY d.id"));
        assert!(params.is_empty());
        let ids = [Uuid::nil()];
        let (sql, params) = structure_candidates_sql(Some(&ids), true);
        assert!(sql.contains("d.id = ANY($1)"));
        assert!(sql.contains("NOT EXISTS"));
        assert_eq!(params, [SqlParam::UuidList(vec![Uuid::nil()])]);
        assert_eq!(
            dependent_count_sql("mentions", "passage_id"),
            "SELECT COUNT(*) FROM core.mentions WHERE passage_id = $1"
        );
    }
}
