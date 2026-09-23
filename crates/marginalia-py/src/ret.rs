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

use marginalia_ret::eval as ret_eval;
use marginalia_ret::filters as ret_filters;
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
    ] {
        m.add_function(f).expect("module attribute assignment");
    }
}

#[cfg(test)]
mod tests {
    use super::{
        build_keyword_search_sql, dcg, english_reference, like_escape, map_empty_note, ndcg_at_k,
        over_limit_note, partials_note, precision_at_k, qere_note, recall_at_k, reciprocal_rank,
        register_ret, validate_filters, zero_result_note,
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
            ] {
                assert!(m.hasattr(name).unwrap(), "missing {name}");
            }
        });
    }
}
