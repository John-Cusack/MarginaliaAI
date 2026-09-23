//! Retrieval-evaluation, filter-SQL, and words-shaping bindings over `marginalia-ret`.
//!
//! Crossing contract (all pinned by tests):
//! - Passage ids cross as canonical UUID strings (parsed back with
//!   `Uuid::parse_str`; anything else answers `ValueError`). Outputs are
//!   floats — no identity to preserve.
//! - `ndcg_at_k` takes relevance either as an id list (binary) or as an
//!   id→grade mapping, mirroring the Python `isinstance` branch; both arms
//!   are fired by tests.
//! - Floats cross as binary `f64`. `recall`/`precision`/`reciprocal_rank`
//!   are exactly rounded IEEE division on both sides; `dcg` is a plain
//!   fold where CPython uses Neumaier-compensated `sum()` (documented in
//!   the crate: it feeds comparisons, never truncation), so long gain
//!   lists may differ by 1 ulp — the parity suite measures the boundary
//!   rather than asserting blind bit-equality there.
//! - Filter SQL crosses as plain strings. `build_keyword_search_sql` fails
//!   only as `InvalidQuery`, whose `invalid query: ` Display prefix is
//!   stripped so the Python `ValueError` text crosses verbatim.
//! - `like_escape` is total: backslash, then `%`, then `_`, exactly like
//!   the Python chained replaces.
//! - `validate_filters` raises the real `research_engine.domain.errors`
//!   classes (imported, not re-implemented): type, attributes, and message
//!   identical by construction.
//! - Words shaping crosses as plain strings. `english_reference` renders
//!   the outcome dict with the same keys in the same order; its `mapping`
//!   value is the lowercase outcome (`full`/`partial`/`same`/`unmapped`),
//!   matching the raw strings Python passes through. A `mapping_type` the
//!   schema forbids answers `ValueError` here where Python would echo it
//!   (pinned typed boundary: the column is constrained to
//!   `'full'`/`'partial'`).
//! - Audit refs cross as string lists. `normalize_audit_refs` strips,
//!   refuses empties, and dedupes in order; `first_missing_ref` answers
//!   the first checked ref outside the known set (or null). Non-string
//!   refs answer `TypeError` here where Python raises `ValueError`
//!   (pinned typed boundary: the signature promises strings).
//! - Context `slice_window` stays unseamed: it needs the full document
//!   length, which `many_for_coordinates` never fetches (it assembles
//!   `window_end` from the fetched slice instead) — a fetch-shape
//!   mismatch, not a seam. The assembly stays Python.
//! - Backfill routing crosses as plain values. `is_pack_uri` is the
//!   pack-URI check verbatim; `classify_route` takes the I/O probes as
//!   parameters (file flag, dispatch outcome tuple) and answers the route
//!   triple as a dict; `validate_recovered_text` takes the parsed text
//!   (null fails like empty); `resolve_language` prefers the supplied
//!   language exactly like `supplied or default`. The route renders
//!   through the crate's own serde contract, so no outcome spelling lives
//!   on this side.

use std::collections::HashSet;

use marginalia_ret::argument as ret_argument;
use marginalia_ret::eval as ret_eval;
use marginalia_ret::filters as ret_filters;
use marginalia_ret::ingest as ret_ingest;
use marginalia_ret::words as ret_words;
use pyo3::call::PyCallArgs;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use uuid::Uuid;

/// Parse canonical UUID strings.
///
/// # Errors
///
/// Returns `ValueError` on the first unparseable id.
fn parse_ids(ids: &[String]) -> PyResult<Vec<Uuid>> {
    ids.iter()
        .map(|s| {
            Uuid::parse_str(s).map_err(|_| {
                PyValueError::new_err(format!("marginalia_rs.ret expects UUID ids, got {s:?}"))
            })
        })
        .collect()
}

/// Fraction of relevant passages in the top *k* (1.0 when none judged).
/// Mirrors `eval/metrics.py::recall_at_k` exactly.
#[pyfunction]
fn recall_at_k(retrieved: Vec<String>, relevant: Vec<String>, k: usize) -> PyResult<f64> {
    Ok(ret_eval::recall_at_k(
        &parse_ids(&retrieved)?,
        &parse_ids(&relevant)?,
        k,
    ))
}

/// Fraction of the top *k* that is relevant (0.0 when `k == 0`).
/// Mirrors `eval/metrics.py::precision_at_k` exactly.
#[pyfunction]
fn precision_at_k(retrieved: Vec<String>, relevant: Vec<String>, k: usize) -> PyResult<f64> {
    Ok(ret_eval::precision_at_k(
        &parse_ids(&retrieved)?,
        &parse_ids(&relevant)?,
        k,
    ))
}

/// 1/rank of the first relevant result; 0.0 if none is retrieved.
/// Mirrors `eval/metrics.py::reciprocal_rank` exactly.
#[pyfunction]
fn reciprocal_rank(retrieved: Vec<String>, relevant: Vec<String>) -> PyResult<f64> {
    Ok(ret_eval::reciprocal_rank(
        &parse_ids(&retrieved)?,
        &parse_ids(&relevant)?,
    ))
}

/// Discounted cumulative gain over `gains`.
/// Mirrors `eval/metrics.py::dcg` up to the documented fold-vs-Neumaier
/// boundary (see module notes).
#[pyfunction]
fn dcg(gains: Vec<f64>) -> f64 {
    // `Iterator::sum` answers `-0.0` on an empty fold while Python's
    // `sum([])` is int `0`: `+ 0.0` canonicalizes the zero without
    // touching any other value (IEEE identity except on `-0.0`).
    ret_eval::dcg(&gains) + 0.0
}

/// Normalised discounted cumulative gain over the top *k*.
///
/// `relevant` is an id list (binary) or an id→grade mapping, exactly like
/// upstream. Mirrors `eval/metrics.py::ndcg_at_k` up to the same boundary.
///
/// # Errors
///
/// Returns `ValueError` on unparseable ids.
#[pyfunction]
fn ndcg_at_k(retrieved: Vec<String>, relevant: Bound<'_, PyAny>, k: usize) -> PyResult<f64> {
    let retrieved = parse_ids(&retrieved)?;
    if let Ok(mapping) = relevant.downcast::<PyDict>() {
        let mut pairs: Vec<(Uuid, f64)> = Vec::new();
        for (key, value) in mapping.iter() {
            let id: String = key.extract().map_err(|_| {
                PyValueError::new_err("marginalia_rs.ret expects UUID ids".to_owned())
            })?;
            let grade: f64 = value.extract().map_err(|_| {
                PyValueError::new_err("marginalia_rs.ret expects float grades".to_owned())
            })?;
            pairs.push((
                Uuid::parse_str(&id).map_err(|_| {
                    PyValueError::new_err(format!("marginalia_rs.ret expects UUID ids, got {id:?}"))
                })?,
                grade,
            ));
        }
        Ok(ret_eval::ndcg_at_k(
            &retrieved,
            ret_eval::NdcgRelevant::Graded(&pairs),
            k,
        ))
    } else {
        let ids: Vec<String> = relevant.extract().map_err(|_| {
            PyValueError::new_err(
                "marginalia_rs.ret expects an id list or id→grade mapping".to_owned(),
            )
        })?;
        Ok(ret_eval::ndcg_at_k(
            &retrieved,
            ret_eval::NdcgRelevant::Binary(&parse_ids(&ids)?),
            k,
        ))
    }
}

/// One indexed keyword-search branch per language config, unioned.
/// Mirrors `passages.py::build_keyword_search_sql` byte-for-byte, including
/// the `ValueError` texts.
///
/// # Errors
///
/// Returns `ValueError` when `configs` is empty or holds an unvalidated
/// regconfig. `Error::InvalidQuery` Displays as `invalid query: {msg}`;
/// the prefix is stripped so the Python text crosses verbatim (the parity
/// suite pins the exact strings; `unwrap_or` keeps a prefix-less future
/// message readable instead of untranslatable).
#[pyfunction]
fn build_keyword_search_sql(configs: Vec<String>) -> PyResult<String> {
    let refs: Vec<&str> = configs.iter().map(String::as_str).collect();
    ret_filters::build_keyword_search_sql(&refs).map_err(|e| {
        let text = e.to_string();
        let msg = text.strip_prefix("invalid query: ").unwrap_or(&text);
        PyValueError::new_err(msg.to_owned())
    })
}

#[pyfunction]
fn like_escape(value: &str) -> String {
    ret_filters::like_escape(value)
}

/// Reject filter keys and extension ids that would otherwise be ignored.
/// Mirrors `passages.py::validate_filters`: unknown keys raise with the
/// sorted unknown list and the sorted supported list; a requested extension
/// id missing from `available_extensions` raises with the sorted available
/// list (or the no-registry hint when none exist).
///
/// The raised objects are the real `research_engine.domain.errors` classes
/// (imported, not re-implemented), so type, attributes, and message text
/// are identical by construction — there is no message rendering on this
/// side to drift. The imports are `expect`ed in the proven-infallible
/// class: the seam runs inside the package that owns those modules.
///
/// # Errors
///
/// Returns the constructed `UnsupportedFilterError` / `UnknownFilterExtension`.
#[pyfunction]
fn validate_filters(
    py: Python<'_>,
    filter_keys: Vec<String>,
    extension_ids: Vec<String>,
    available_extensions: Vec<String>,
) -> PyResult<()> {
    let keys: Vec<&str> = filter_keys.iter().map(String::as_str).collect();
    let exts: Vec<&str> = extension_ids.iter().map(String::as_str).collect();
    let available: Vec<&str> = available_extensions.iter().map(String::as_str).collect();
    match ret_filters::validate_filters(&keys, &exts, &available) {
        Ok(()) => Ok(()),
        Err(ret_filters::FilterValidation::UnknownKeys { unknown, supported }) => Err(
            domain_error(py, "UnsupportedFilterError", (unknown, supported)),
        ),
        Err(ret_filters::FilterValidation::UnknownExtension { id, available }) => {
            Err(domain_error(py, "UnknownFilterExtension", (id, available)))
        }
    }
}

/// Construct a `research_engine.domain.errors` exception from its `__init__` args.
fn domain_error<'py, A>(py: Python<'py>, class: &str, args: A) -> PyErr
where
    A: PyCallArgs<'py>,
{
    let errors = PyModule::import(py, "research_engine.domain.errors")
        .expect("core errors module is importable from the seam");
    let cls = errors.getattr(class).expect("domain error class exists");
    let instance = cls
        .call1(args)
        .expect("domain error __init__ signature matches");
    PyErr::from_value(instance)
}

/// Render one occurrence's English-side reference.
/// Mirrors `words/lookup.py::english_reference`: the same four keys in the
/// same order, with the lowercase outcome as `mapping`.
///
/// # Errors
///
/// Returns `ValueError` when a loaded map joins a row whose `mapping_type`
/// the schema forbids (Python would echo the raw string; the column is
/// constrained to `'full'`/`'partial'`, so this is a typed boundary, not a
/// reachable path). The decode runs only on the mapped arm — the unmapped
/// and same arms ignore `mapping_type` exactly like the Python branches.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn english_reference<'py>(
    py: Python<'py>,
    hebrew_ref: &str,
    to_ref: Option<String>,
    to_part: Option<String>,
    from_part: Option<String>,
    mapping_type: Option<String>,
    map_loaded: bool,
) -> PyResult<Py<PyDict>> {
    let mapping = if !map_loaded || to_ref.is_none() {
        None
    } else {
        Some(
            ret_words::Mapping::from_mapping_type(mapping_type.as_deref().unwrap_or("")).map_err(
                |e| {
                    let text = e.to_string();
                    let msg = text.strip_prefix("invalid query: ").unwrap_or(&text);
                    PyValueError::new_err(msg.to_owned())
                },
            )?,
        )
    };
    let english = ret_words::english_reference(
        hebrew_ref,
        to_ref.as_deref(),
        to_part.as_deref(),
        from_part.as_deref(),
        mapping,
        map_loaded,
    );
    let outcome = match english.mapping {
        ret_words::Mapping::Unmapped => "unmapped",
        ret_words::Mapping::Same => "same",
        ret_words::Mapping::Full => "full",
        ret_words::Mapping::Partial => "partial",
    };
    let out = PyDict::new(py);
    out.set_item("ref", english.r#ref.as_deref())
        .expect("str keys into a fresh dict");
    out.set_item("mapping", outcome)
        .expect("str keys into a fresh dict");
    out.set_item("part", english.part.as_deref())
        .expect("str keys into a fresh dict");
    out.set_item("hebrew_part", english.hebrew_part.as_deref())
        .expect("str keys into a fresh dict");
    Ok(out.unbind())
}

/// The zero-result hint: which number in which lexicon missed, and how to widen.
/// Mirrors the `find()` inline note, except the homograph renders raw where
/// Python uses `{homograph!r}` (pinned typed boundary: homographs are
/// single letters, never quoted strings).
#[pyfunction]
fn zero_result_note(strong: &str, language: &str, homograph: Option<String>) -> String {
    ret_words::zero_result_note(&ret_words::LemmaQuery {
        strong: strong.to_owned(),
        language: language.to_owned(),
        homograph,
        book: None,
        chapter_start: None,
        chapter_end: None,
        include_occurrences: true,
    })
}

/// First note when the verse map is empty. Mirrors the `find()` inline text.
#[pyfunction]
fn map_empty_note() -> &'static str {
    ret_words::map_empty_note()
}

/// Refusal past `MAX_OCCURRENCES`. Mirrors the `find()` inline text.
#[pyfunction]
fn over_limit_note(total: i64) -> String {
    ret_words::over_limit_note(total)
}

/// Attached when partials are present. Mirrors the `find()` inline text.
#[pyfunction]
fn partials_note(count: usize) -> String {
    ret_words::partials_note(count)
}

/// Attached when qere-sourced occurrences are present: first 6 refs, then an
/// ellipsis. Takes every qere ref (like the `find()` inline join over the
/// full list); mirrors its text exactly.
#[pyfunction]
fn qere_note(refs: Vec<String>) -> String {
    ret_words::qere_note(&refs)
}

/// Pure claim-edge check: duplicate (target, relation) pairs and self-edges
/// refuse; anything else — including an empty edge list — passes. Mirrors
/// `ClaimService._validate_edges`; target *existence* needs the database
/// and raises via [`missing_target_refusal`].
///
/// The raised objects are the real `ClaimWriteRefused` class (imported from
/// `services.argument.claims`, not re-implemented): code, message, and the
/// `edge_index` detail are identical by construction. The import chain is
/// `expect`ed in the proven-infallible class.
///
/// # Errors
///
/// Returns the constructed `ClaimWriteRefused`.
#[pyfunction]
fn validate_claim_edges(
    py: Python<'_>,
    own_ref: &str,
    edges: Vec<(String, String)>,
) -> PyResult<()> {
    let drafts: Vec<ret_argument::ClaimEdgeDraft> = edges
        .iter()
        .map(|(target_ref, relation)| ret_argument::ClaimEdgeDraft {
            target_ref: target_ref.clone(),
            relation: relation.clone(),
        })
        .collect();
    match ret_argument::validate_claim_edges(own_ref, &drafts) {
        Ok(()) => Ok(()),
        Err(refused) => Err(claim_refused(py, refused)),
    }
}

/// Refusal for the target-existence branch of `ClaimService.upsert`
/// (`code: "not_found"`); the lookup itself stays in Python and calls this
/// to build the refusal it raises. Always fails — the success case is the
/// caller finding the target.
///
/// # Errors
///
/// Always returns the constructed `ClaimWriteRefused`.
#[pyfunction]
fn missing_target_refusal(py: Python<'_>, target_ref: &str) -> PyResult<()> {
    Err(claim_refused(
        py,
        ret_argument::missing_target_refusal(target_ref),
    ))
}

/// Raise the real `services.argument.claims.ClaimWriteRefused` from a
/// crate-side refusal: code and message cross as strings, the detail map
/// crosses as JSON (total over every value shape — today `edge_index` ints
/// and `target_ref` strs — so a new shape needs no seam change; refusals
/// are rare, so the round-trip costs nothing hot).
fn claim_refused(py: Python<'_>, refused: ret_argument::ClaimWriteRefused) -> PyErr {
    let errors = PyModule::import(py, "research_engine.services.argument.claims")
        .expect("claim service module is importable from the seam");
    let cls = errors
        .getattr("ClaimWriteRefused")
        .expect("ClaimWriteRefused class exists");
    let json = serde_json::to_string(&refused.detail).expect("detail map serializes");
    let json_mod = PyModule::import(py, "json").expect("stdlib json is importable");
    let detail = json_mod
        .call_method1("loads", (json,))
        .expect("seam-serialized JSON parses");
    let instance = cls
        .call1((refused.code, refused.message, detail))
        .expect("ClaimWriteRefused __init__ signature matches");
    PyErr::from_value(instance)
}

/// Normalise audit subjects: strip, refuse empties, dedupe preserving
/// order. Mirrors the `refs` prelude of `ClaimAuditService.audit`
/// (including the refusal text verbatim).
///
/// # Errors
///
/// Returns `ValueError` when a ref is empty after stripping.
#[pyfunction]
fn normalize_audit_refs(refs: Vec<String>) -> PyResult<Vec<String>> {
    let candidates: Vec<&str> = refs.iter().map(String::as_str).collect();
    ret_argument::normalize_audit_refs(&candidates).map_err(|e| {
        let text = e.to_string();
        let msg = text.strip_prefix("invalid query: ").unwrap_or(&text);
        PyValueError::new_err(msg.to_owned())
    })
}

/// First checked ref absent from the known set, or null when all resolve.
/// Mirrors the `missing` branch of `ClaimAuditService.audit`; the adapter
/// raises `NotFoundError` on a hit, exactly like the Python path.
#[pyfunction]
fn first_missing_ref(checked: Vec<String>, existing: Vec<String>) -> Option<String> {
    let known: HashSet<String> = existing.into_iter().collect();
    ret_argument::first_missing_ref(&checked, &known)
}

/// A source that is not a filesystem path can only be re-fetched by the
/// pack that produced it. Mirrors the pack-URI check in
/// `text_backfill._classify` verbatim: `://` anywhere, or a `:` in a path
/// that does not start at the root.
#[pyfunction]
fn is_pack_uri(source: &str) -> bool {
    ret_ingest::is_pack_uri(source)
}

/// Classify one document lacking canonical text by recovery route.
///
/// `file_exists` and `dispatch` stand in for the filesystem and dispatcher
/// probes, which stay in the adapter: it only consults the dispatcher when
/// the file exists, mirroring the order of checks. `dispatch` is
/// `(accepted, value)` — the module id when accepted, `str(exc)` when no
/// module takes the source. Answers the route triple as a dict; the route
/// renders through the crate's own serde contract.
#[pyfunction]
fn classify_route(
    py: Python<'_>,
    source: &str,
    file_exists: bool,
    dispatch: (bool, String),
) -> PyResult<Py<PyDict>> {
    let outcome: Result<&str, &str> = if dispatch.0 {
        Ok(dispatch.1.as_str())
    } else {
        Err(dispatch.1.as_str())
    };
    let classified = ret_ingest::classify_route(source, file_exists, outcome);
    let route = serde_json::to_value(classified.route)
        .expect("Route serializes")
        .as_str()
        .expect("Route serializes as a string")
        .to_owned();
    let out = PyDict::new(py);
    out.set_item("route", route)
        .expect("str keys into a fresh dict");
    out.set_item("module_id", classified.module_id.as_deref())
        .expect("str keys into a fresh dict");
    out.set_item("detail", classified.detail)
        .expect("str keys into a fresh dict");
    Ok(out.unbind())
}

/// Reject an empty recovery before it is stored: without text the offsets
/// would address nothing. Mirrors the `_recover_one` guard verbatim;
/// a null parse result fails the same way as empty text, exactly like
/// the Python `not full_text or ...`.
///
/// # Errors
///
/// Returns `ValueError` (`"parser produced no text"`) on empty text.
#[pyfunction]
fn validate_recovered_text(full_text: Option<String>) -> PyResult<String> {
    let text = full_text.as_deref().unwrap_or("");
    ret_ingest::validate_recovered_text(text)
        .map(str::to_string)
        .map_err(|message| PyValueError::new_err(message.to_owned()))
}

/// Prefer what the caller or parser knows; otherwise the configured
/// default. Mirrors `Orchestrator._resolve_language` exactly: an empty
/// `supplied` falls back like a missing one (`supplied or default`).
#[pyfunction]
fn resolve_language(supplied: Option<String>, default: Option<String>) -> Option<String> {
    if let Some(known) = supplied {
        if !known.is_empty() {
            return Some(known);
        }
    }
    default
}

pub fn ret_module(py: Python<'_>) -> Bound<'_, PyModule> {
    let m = PyModule::new(py, "ret").expect("module name is a valid literal");
    register_ret(&m);
    m
}

pub fn register_ret(m: &Bound<'_, PyModule>) {
    for f in [
        wrap_pyfunction!(recall_at_k, m).expect("function name is a unique literal"),
        wrap_pyfunction!(precision_at_k, m).expect("function name is a unique literal"),
        wrap_pyfunction!(reciprocal_rank, m).expect("function name is a unique literal"),
        wrap_pyfunction!(dcg, m).expect("function name is a unique literal"),
        wrap_pyfunction!(ndcg_at_k, m).expect("function name is a unique literal"),
        wrap_pyfunction!(build_keyword_search_sql, m).expect("function name is a unique literal"),
        wrap_pyfunction!(like_escape, m).expect("function name is a unique literal"),
        wrap_pyfunction!(validate_filters, m).expect("function name is a unique literal"),
        wrap_pyfunction!(english_reference, m).expect("function name is a unique literal"),
        wrap_pyfunction!(zero_result_note, m).expect("function name is a unique literal"),
        wrap_pyfunction!(map_empty_note, m).expect("function name is a unique literal"),
        wrap_pyfunction!(over_limit_note, m).expect("function name is a unique literal"),
        wrap_pyfunction!(partials_note, m).expect("function name is a unique literal"),
        wrap_pyfunction!(qere_note, m).expect("function name is a unique literal"),
        wrap_pyfunction!(validate_claim_edges, m).expect("function name is a unique literal"),
        wrap_pyfunction!(missing_target_refusal, m).expect("function name is a unique literal"),
        wrap_pyfunction!(normalize_audit_refs, m).expect("function name is a unique literal"),
        wrap_pyfunction!(first_missing_ref, m).expect("function name is a unique literal"),
        wrap_pyfunction!(is_pack_uri, m).expect("function name is a unique literal"),
        wrap_pyfunction!(classify_route, m).expect("function name is a unique literal"),
        wrap_pyfunction!(validate_recovered_text, m).expect("function name is a unique literal"),
        wrap_pyfunction!(resolve_language, m).expect("function name is a unique literal"),
    ] {
        m.add_function(f).expect("module attribute assignment");
    }
}

#[cfg(test)]
mod tests {
    use super::{
        build_keyword_search_sql, classify_route, dcg, english_reference, first_missing_ref,
        is_pack_uri, like_escape, map_empty_note, missing_target_refusal, ndcg_at_k,
        normalize_audit_refs, over_limit_note, partials_note, precision_at_k, qere_note,
        recall_at_k, reciprocal_rank, register_ret, resolve_language, validate_claim_edges,
        validate_filters, validate_recovered_text, zero_result_note,
    };
    use pyo3::prelude::*;
    use pyo3::types::{PyDict, PyList};

    fn ids(n: usize) -> Vec<String> {
        (0..n)
            .map(|i| format!("12345678-1234-5678-1234-56781234{i:04}"))
            .collect()
    }

    #[test]
    fn metrics_match_crate_on_rank_shapes() {
        let all = ids(10);
        let retrieved = all[..6].to_vec();
        let relevant = all[2..8].to_vec();
        assert_eq!(
            recall_at_k(retrieved.clone(), relevant.clone(), 10)
                .unwrap()
                .to_bits(),
            marginalia_ret::eval::recall_at_k(&parsed(&retrieved), &parsed(&relevant), 10)
                .to_bits()
        );
        assert_eq!(
            precision_at_k(retrieved.clone(), relevant.clone(), 0)
                .unwrap()
                .to_bits(),
            0.0f64.to_bits()
        );
        assert_eq!(
            reciprocal_rank(retrieved.clone(), relevant.clone())
                .unwrap()
                .to_bits(),
            marginalia_ret::eval::reciprocal_rank(&parsed(&retrieved), &parsed(&relevant))
                .to_bits()
        );
        assert!(recall_at_k(vec!["nope".to_owned()], relevant.clone(), 10).is_err());
        assert!(recall_at_k(retrieved.clone(), vec!["nope".to_owned()], 10).is_err());
        assert!(precision_at_k(retrieved.clone(), vec!["nope".to_owned()], 10).is_err());
        assert!(precision_at_k(vec!["nope".to_owned()], relevant.clone(), 10).is_err());
        assert!(reciprocal_rank(retrieved.clone(), vec!["nope".to_owned()]).is_err());
        assert!(reciprocal_rank(vec!["nope".to_owned()], relevant.clone()).is_err());
        assert!(recall_at_k(vec!["nope".to_owned()], relevant, 10).is_err());
    }

    fn parsed(ids: &[String]) -> Vec<uuid::Uuid> {
        ids.iter()
            .map(|s| uuid::Uuid::parse_str(s).unwrap())
            .collect()
    }

    #[test]
    fn dcg_matches_crate_on_short_lists() {
        // Empty canonicalizes `-0.0` (fold) to `+0.0` (Python `sum`); the
        // rest mirror the crate bit-for-bit.
        assert_eq!(dcg(vec![]).to_bits(), 0.0f64.to_bits());
        for gains in [vec![3.0], vec![3.0, 2.0, 1.0], vec![0.5; 20]] {
            assert_eq!(
                dcg(gains.clone()).to_bits(),
                marginalia_ret::eval::dcg(&gains).to_bits()
            );
        }
    }

    #[test]
    fn ndcg_handles_binary_and_graded() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let all = ids(6);
            let retrieved = all[..4].to_vec();
            let binary: Vec<String> = all[1..5].to_vec();
            let as_any: Bound<'_, PyAny> = PyList::new(py, &binary).unwrap().into_any();
            let got = ndcg_at_k(retrieved.clone(), as_any, 10).unwrap();
            let want = marginalia_ret::eval::ndcg_at_k(
                &parsed(&retrieved),
                marginalia_ret::eval::NdcgRelevant::Binary(&parsed(&binary)),
                10,
            );
            assert_eq!(got.to_bits(), want.to_bits());
            let graded = PyDict::new(py);
            graded.set_item(&all[1], 2.0).unwrap();
            graded.set_item(&all[2], 1.0).unwrap();
            let got = ndcg_at_k(retrieved.clone(), graded.into_any(), 10).unwrap();
            assert!(got > 0.0);
            // Empty judgments score 1.0 on either shape.
            let empty_list: Bound<'_, PyAny> = PyList::empty(py).into_any();
            assert_eq!(
                ndcg_at_k(retrieved.clone(), empty_list, 10)
                    .unwrap()
                    .to_bits(),
                1.0f64.to_bits()
            );
            let empty_dict: Bound<'_, PyAny> = PyDict::new(py).into_any();
            assert_eq!(
                ndcg_at_k(retrieved, empty_dict, 10).unwrap().to_bits(),
                1.0f64.to_bits()
            );
            // Garbage shapes are ValueErrors, not panics.
            let bad: Bound<'_, PyAny> = 42u32.into_pyobject(py).unwrap().into_any();
            assert!(ndcg_at_k(vec![], bad, 10).is_err());
            let bad_ids: Bound<'_, PyAny> = PyList::new(py, ["nope"]).unwrap().into_any();
            assert!(ndcg_at_k(vec![], bad_ids, 10).is_err());
            let bad_key = PyDict::new(py);
            bad_key.set_item(42u32, 1.0).unwrap();
            assert!(ndcg_at_k(vec![], bad_key.into_any(), 10).is_err());
            let bad_grade = PyDict::new(py);
            bad_grade.set_item(&all[1], "high").unwrap();
            assert!(ndcg_at_k(vec![], bad_grade.into_any(), 10).is_err());
            let bad_uuid = PyDict::new(py);
            bad_uuid.set_item("nope", 1.0).unwrap();
            assert!(ndcg_at_k(vec![], bad_uuid.into_any(), 10).is_err());
            let empty_list: Bound<'_, PyAny> = PyList::empty(py).into_any();
            assert!(ndcg_at_k(vec!["nope".to_owned()], empty_list, 10).is_err());
        });
    }

    #[test]
    fn keyword_sql_matches_crate_byte_for_byte() {
        pyo3::prepare_freethreaded_python();
        // Rendering a `PyErr` needs the GIL even though no Python objects cross.
        Python::with_gil(|_py| {
            for configs in [
                vec!["english".to_owned()],
                vec!["english".to_owned(), "german".to_owned()],
                vec!["english".to_owned(), "english".to_owned()],
            ] {
                let refs: Vec<&str> = configs.iter().map(String::as_str).collect();
                assert_eq!(
                    build_keyword_search_sql(configs.clone()).unwrap(),
                    marginalia_ret::filters::build_keyword_search_sql(&refs).unwrap()
                );
            }
            // Empty answers the Python `ValueError` text verbatim (no Display prefix).
            // (`PyErr` display prepends the type name; the `str(exc)` the caller
            // sees carries just the message.)
            assert_eq!(
                build_keyword_search_sql(vec![]).unwrap_err().to_string(),
                "ValueError: build_keyword_search_sql requires at least one config"
            );
            // Unvalidated regconfigs render as a Python list repr.
            let err = build_keyword_search_sql(vec!["english".to_owned(), "xx;q".to_owned()])
                .unwrap_err()
                .to_string();
            assert_eq!(
                err,
                "ValueError: refusing to interpolate unvalidated regconfig(s): ['xx;q']"
            );
        });
    }

    #[test]
    fn like_escape_matches_crate_on_metacharacters() {
        for value in [
            "",
            "plain quote",
            "100% coverage",
            "snake_case_name",
            "back\\slash",
            "%_%\\ mixed",
            "hébreu 100%_שלום",
        ] {
            assert_eq!(
                like_escape(value),
                marginalia_ret::filters::like_escape(value)
            );
        }
    }

    #[test]
    fn validation_accepts_known_keys() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            assert!(validate_filters(
                py,
                vec!["language".to_owned(), "extensions".to_owned()],
                vec!["has_extraction".to_owned()],
                vec!["has_extraction".to_owned()],
            )
            .is_ok());
            assert!(validate_filters(py, vec![], vec![], vec![],).is_ok());
        });
    }

    #[test]
    fn validation_raises_the_domain_types() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let errors = PyModule::import(py, "research_engine.domain.errors").unwrap();
            // Unknown keys: sorted, deduped, with the sorted supported list.
            let err = validate_filters(
                py,
                vec!["zzz".to_owned(), "aaa".to_owned(), "zzz".to_owned()],
                vec![],
                vec![],
            )
            .unwrap_err();
            let unsupported = errors.getattr("UnsupportedFilterError").unwrap();
            let value = err.value(py);
            assert!(value.is_instance(&unsupported).unwrap());
            let unknown: Vec<String> = value.getattr("unknown").unwrap().extract().unwrap();
            assert_eq!(unknown, vec!["aaa".to_owned(), "zzz".to_owned()]);
            let supported: Vec<String> = value.getattr("supported").unwrap().extract().unwrap();
            assert!(supported.contains(&"language".to_owned()));
            // Unknown extension: id plus the sorted available list.
            let err = validate_filters(
                py,
                vec!["extensions".to_owned()],
                vec!["nope".to_owned()],
                vec!["has_extraction".to_owned()],
            )
            .unwrap_err();
            let unknown_ext = errors.getattr("UnknownFilterExtension").unwrap();
            let value = err.value(py);
            assert!(value.is_instance(&unknown_ext).unwrap());
            let ext_id: String = value.getattr("extension_id").unwrap().extract().unwrap();
            assert_eq!(ext_id, "nope");
            // Empty registry selects the no-extensions hint branch.
            let err = validate_filters(
                py,
                vec!["extensions".to_owned()],
                vec!["nope".to_owned()],
                vec![],
            )
            .unwrap_err();
            assert!(err
                .to_string()
                .contains("No filter extensions are registered"));
        });
    }

    #[test]
    fn english_reference_matches_crate_on_outcomes() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            // Unmapped: no map to consult, ref withheld even with row data.
            let got = english_reference(
                py,
                "Gen.1.1",
                Some("Gen.1.1".to_owned()),
                Some("a".to_owned()),
                Some("b".to_owned()),
                Some("full".to_owned()),
                false,
            )
            .unwrap();
            let got = got.bind(py);
            let get = |key: &str| got.get_item(key).unwrap().unwrap();
            assert_eq!(get("ref").extract::<Option<String>>().unwrap(), None);
            assert_eq!(get("mapping").extract::<String>().unwrap(), "unmapped");
            // Same: map loaded, no row.
            let got = english_reference(py, "Gen.1.1", None, None, None, None, true).unwrap();
            let got = got.bind(py);
            let get = |key: &str| got.get_item(key).unwrap().unwrap();
            assert_eq!(get("ref").extract::<String>().unwrap(), "Gen.1.1");
            assert_eq!(get("mapping").extract::<String>().unwrap(), "same");
            // Full and partial carry the row through.
            for (mapping, part) in [("full", None), ("partial", Some("a"))] {
                let got = english_reference(
                    py,
                    "Gen.1.2",
                    Some("Gen.1.3".to_owned()),
                    part.map(str::to_string),
                    Some("b".to_owned()),
                    Some(mapping.to_owned()),
                    true,
                )
                .unwrap();
                let got = got.bind(py);
                let get = |key: &str| got.get_item(key).unwrap().unwrap();
                assert_eq!(get("mapping").extract::<String>().unwrap(), mapping);
                assert_eq!(
                    get("part").extract::<Option<String>>().unwrap(),
                    part.map(str::to_string)
                );
            }
            // Keys render in the Python literal order.
            let keys: Vec<String> = got
                .keys()
                .iter()
                .map(|k| k.extract::<String>().unwrap())
                .collect();
            assert_eq!(keys, vec!["ref", "mapping", "part", "hebrew_part"]);
            // A mapping_type the schema forbids answers ValueError.
            let err = english_reference(
                py,
                "Gen.1.1",
                Some("Gen.1.2".to_owned()),
                None,
                None,
                Some("weird".to_owned()),
                true,
            )
            .unwrap_err()
            .to_string();
            assert!(err.contains("unknown verse_map mapping_type"), "{err}");
            // ... as does a joined row missing its mapping.
            assert!(english_reference(
                py,
                "Gen.1.1",
                Some("Gen.1.2".to_owned()),
                None,
                None,
                None,
                true
            )
            .is_err());
        });
    }

    #[test]
    fn words_notes_match_crate_texts() {
        assert_eq!(
            zero_result_note("4941", "he", None),
            marginalia_ret::words::zero_result_note(&marginalia_ret::words::LemmaQuery::new(
                "4941"
            ))
        );
        assert_eq!(
            zero_result_note("4941", "he", Some("a".to_owned())),
            "No word in 'he' carries Strong's 4941 with homograph 'a'. Check the number, or drop the homograph to widen."
        );
        assert_eq!(
            zero_result_note("4941", "he", Some(String::new())),
            "No word in 'he' carries Strong's 4941. Check the number, or drop the homograph to widen."
        );
        assert_eq!(map_empty_note(), marginalia_ret::words::map_empty_note());
        assert_eq!(
            over_limit_note(2500),
            "2500 occurrences is over the 2000 limit; narrow with book or chapters, or read counts instead. No occurrences returned."
        );
        assert_eq!(
            partials_note(3),
            "3 occurrence(s) sit in a verse the English tradition splits or joins; their english.ref is the verse the text begins in, and english.part says which half. Cite the Hebrew reference unless you are quoting an English edition."
        );
        assert_eq!(
            qere_note(vec!["Gen.1.1".to_owned(), "Ex.2.2".to_owned()]),
            "2 occurrence(s) come from a qere (Gen.1.1, Ex.2.2). The reference is right in every edition, but an edition that prints the ketiv writes a different word there — read the verse before quoting the surface form."
        );
        let many: Vec<String> = (1..=8).map(|i| format!("Gen.1.{i}")).collect();
        let got = qere_note(many);
        assert!(got.starts_with("8 occurrence(s) come from a qere (Gen.1.1, Gen.1.2, Gen.1.3, Gen.1.4, Gen.1.5, Gen.1.6, …)"), "{got}");
    }

    #[test]
    fn claim_edges_accept_clean_lists() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            assert!(validate_claim_edges(
                py,
                "C1",
                vec![
                    ("C2".to_owned(), "supports".to_owned()),
                    ("C2".to_owned(), "contradicts".to_owned()),
                    ("C3".to_owned(), "supports".to_owned()),
                ],
            )
            .is_ok());
            assert!(validate_claim_edges(py, "C1", vec![],).is_ok());
        });
    }

    #[test]
    fn claim_edges_raise_the_domain_refusal() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let claims = PyModule::import(py, "research_engine.services.argument.claims").unwrap();
            let refused = claims.getattr("ClaimWriteRefused").unwrap();
            // Duplicate (target, relation) refuses with the edge index.
            let err = validate_claim_edges(
                py,
                "C1",
                vec![
                    ("C2".to_owned(), "supports".to_owned()),
                    ("C2".to_owned(), "supports".to_owned()),
                ],
            )
            .unwrap_err();
            let value = err.value(py);
            assert!(value.is_instance(&refused).unwrap());
            assert_eq!(
                value.getattr("code").unwrap().extract::<String>().unwrap(),
                "invalid_input"
            );
            let detail: std::collections::HashMap<String, i64> =
                value.getattr("detail").unwrap().extract().unwrap();
            assert_eq!(detail.get("edge_index"), Some(&1));
            assert!(err
                .to_string()
                .contains("Edge 1 duplicates an earlier target and relation."));
            // Self-edges refuse with the quoted ref.
            let err =
                validate_claim_edges(py, "C1", vec![("C1".to_owned(), "supports".to_owned())])
                    .unwrap_err();
            assert!(err
                .to_string()
                .contains("Edge 0 points claim 'C1' at itself."));
            // Missing targets refuse as not_found with the ref detail.
            let err = missing_target_refusal(py, "C9").unwrap_err();
            let value = err.value(py);
            assert!(value.is_instance(&refused).unwrap());
            assert_eq!(
                value.getattr("code").unwrap().extract::<String>().unwrap(),
                "not_found"
            );
            let target: String = value
                .getattr("detail")
                .unwrap()
                .get_item("target_ref")
                .unwrap()
                .extract()
                .unwrap();
            assert_eq!(target, "C9");
            assert!(err
                .to_string()
                .contains("Target claim 'C9' does not exist."));
        });
    }

    #[test]
    fn audit_refs_normalize_and_resolve() {
        // Strip, refuse empties, dedupe preserving order.
        assert_eq!(
            normalize_audit_refs(vec!["  C2  ".to_owned(), "C1".to_owned(), "C2".to_owned()])
                .unwrap(),
            vec!["C2".to_owned(), "C1".to_owned()]
        );
        assert_eq!(normalize_audit_refs(vec![]).unwrap(), Vec::<String>::new());
        // Empty after stripping refuses with the audit text verbatim.
        assert_eq!(
            normalize_audit_refs(vec!["C1".to_owned(), "   ".to_owned()])
                .unwrap_err()
                .to_string(),
            "ValueError: refs must contain non-empty claim refs"
        );
        // First miss wins; all-resolve answers null.
        assert_eq!(
            first_missing_ref(
                vec!["C1".to_owned(), "C2".to_owned(), "C3".to_owned()],
                vec!["C1".to_owned(), "C3".to_owned()],
            ),
            Some("C2".to_owned())
        );
        assert_eq!(
            first_missing_ref(vec!["C1".to_owned()], vec!["C1".to_owned()],),
            None
        );
        assert_eq!(first_missing_ref(vec![], vec!["C1".to_owned()],), None);
    }

    #[test]
    fn pack_uris_detected_like_python() {
        for (source, expected) in [
            ("logos:LLS:ABC:batch:b0000", true),
            ("C:\\docs\\scan.pdf", true),
            ("rel:path/doc.pdf", true),
            ("/abs/path/doc.pdf", false),
            ("plain.md", false),
            ("", false),
        ] {
            assert_eq!(is_pack_uri(source), expected, "{source}");
        }
    }

    #[test]
    fn route_classification_matches_crate() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let get = |out: &Py<PyDict>, key: &str| {
                out.bind(py)
                    .get_item(key)
                    .unwrap()
                    .unwrap()
                    .extract::<Option<String>>()
                    .unwrap()
            };
            // Pack URIs never touch the filesystem.
            let out = classify_route(
                py,
                "logos:LLS:ABC:batch:b0000",
                false,
                (false, String::new()),
            )
            .unwrap();
            let bound = out.bind(py);
            assert_eq!(
                bound
                    .get_item("route")
                    .unwrap()
                    .unwrap()
                    .extract::<String>()
                    .unwrap(),
                "unreachable"
            );
            assert_eq!(
                get(&out, "detail"),
                Some("source is a pack URI; re-run that pack's ingest".to_owned())
            );
            // Missing files name the loss.
            let out = classify_route(py, "/gone/doc.pdf", false, (false, String::new())).unwrap();
            assert_eq!(
                out.bind(py)
                    .get_item("detail")
                    .unwrap()
                    .unwrap()
                    .extract::<String>()
                    .unwrap(),
                "source file no longer exists"
            );
            // Dispatch failures carry the cause.
            let out =
                classify_route(py, "/doc.pdf", true, (false, "bad magic".to_owned())).unwrap();
            let bound = out.bind(py);
            assert_eq!(
                bound
                    .get_item("route")
                    .unwrap()
                    .unwrap()
                    .extract::<String>()
                    .unwrap(),
                "unreachable"
            );
            assert_eq!(
                bound
                    .get_item("detail")
                    .unwrap()
                    .unwrap()
                    .extract::<String>()
                    .unwrap(),
                "no ingestion module accepts this source: bad magic"
            );
            // Accepting modules route by weight.
            for (module, route) in [("docling", "slow"), ("plain_text", "fast")] {
                let out = classify_route(py, "/doc.pdf", true, (true, module.to_owned())).unwrap();
                let bound = out.bind(py);
                assert_eq!(
                    bound
                        .get_item("route")
                        .unwrap()
                        .unwrap()
                        .extract::<String>()
                        .unwrap(),
                    route
                );
                assert_eq!(
                    bound
                        .get_item("module_id")
                        .unwrap()
                        .unwrap()
                        .extract::<String>()
                        .unwrap(),
                    module
                );
            }
        });
    }

    #[test]
    fn recovered_text_and_language_match() {
        assert_eq!(
            validate_recovered_text(Some("text".to_owned())).unwrap(),
            "text"
        );
        assert_eq!(
            validate_recovered_text(None).unwrap_err().to_string(),
            "ValueError: parser produced no text"
        );
        assert_eq!(
            validate_recovered_text(Some("   ".to_owned()))
                .unwrap_err()
                .to_string(),
            "ValueError: parser produced no text"
        );
        assert_eq!(
            resolve_language(Some("he".to_owned()), Some("en".to_owned())),
            Some("he".to_owned())
        );
        assert_eq!(
            resolve_language(Some(String::new()), Some("en".to_owned())),
            Some("en".to_owned())
        );
        assert_eq!(
            resolve_language(None, Some("en".to_owned())),
            Some("en".to_owned())
        );
        assert_eq!(resolve_language(None, None), None);
    }

    #[test]
    fn registration_names_the_metrics() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let m = PyModule::new(py, "ret").unwrap();
            register_ret(&m);
            for name in [
                "recall_at_k",
                "precision_at_k",
                "reciprocal_rank",
                "dcg",
                "ndcg_at_k",
                "build_keyword_search_sql",
                "like_escape",
                "validate_filters",
                "english_reference",
                "zero_result_note",
                "map_empty_note",
                "over_limit_note",
                "partials_note",
                "qere_note",
                "validate_claim_edges",
                "missing_target_refusal",
                "normalize_audit_refs",
                "first_missing_ref",
                "is_pack_uri",
                "classify_route",
                "validate_recovered_text",
                "resolve_language",
            ] {
                assert!(m.hasattr(name).unwrap(), "missing {name}");
            }
        });
    }
}
