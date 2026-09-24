//! Prose and structural chunker bindings over `marginalia-chunk`.
//!
//! Crossing contract (all pinned by tests):
//! - Each function answers draft JSON (one `PassageDraft` per string), built
//!   with `metadata: None`: the crate never reads metadata, and the Python
//!   adapter reattaches the original mappings, so object identity holds and
//!   no metadata float ever crosses a decimal boundary.
//! - Sections cross as one JSON table into `SectionInput` (exactly the keys
//!   upstream reads: `text`, `heading`, `level`, `page`, `char_start`,
//!   `char_end`). Section values must be JSON-native; anything else fails
//!   the adapter's `json.dumps` with `TypeError`, where upstream would carry
//!   the object through. Numbers cross verbatim (`arbitrary_precision`), so
//!   a locator value comes back exactly as the parser wrote it.
//! - `chunk_structural` raises the real `ChunkingError` with the crate's
//!   message; `chunk_prose` re-raises `cap_spans` rejections as `ValueError`
//!   with the identical text. Both error paths are fired by tests.

use marginalia_chunk::{prose_window, structural};
use marginalia_types::sdk::PassageDraft;
use marginalia_types::Error;
use pyo3::exceptions::{PyOverflowError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;

/// A token budget as Python holds it: any `int`. A value past i64 is
/// larger (or smaller) than any text's token count can reach, so it
/// saturates without changing a single boundary.
fn budget(value: &Bound<'_, PyAny>) -> PyResult<i64> {
    match value.extract::<i64>() {
        Ok(n) => Ok(n),
        Err(err) if err.is_instance_of::<PyOverflowError>(value.py()) => {
            let positive = value
                .gt(0)
                .expect("an int that overflowed i64 compares with 0");
            Ok(if positive { i64::MAX } else { i64::MIN })
        }
        Err(err) => Err(err),
    }
}

fn draft_json(draft: &PassageDraft) -> String {
    serde_json::to_string(draft).expect("drafts hold JSON-native values")
}

/// Map a prose failure to its Python error.
///
/// `cap_spans` rejections surface as `ValueError` with the identical text.
/// Any other variant would be a crate bug; it degrades to `RuntimeError`
/// rather than silently, pinned by the fault test below.
fn prose_error(err: Error) -> PyErr {
    match err {
        Error::Validation(msg) => PyValueError::new_err(msg),
        other => PyRuntimeError::new_err(format!("unexpected prose error: {other:?}")),
    }
}

/// Sentence-boundary windows over `text`.
///
/// Mirrors `ProseWindowChunker.chunk` without the metadata passthrough.
///
/// # Errors
///
/// Returns `ValueError` when `max_tokens < 1`, with the identical text.
#[pyfunction]
#[pyo3(signature = (text, max_tokens, overlap_tokens))]
fn chunk_prose(
    text: &str,
    max_tokens: &Bound<'_, PyAny>,
    overlap_tokens: &Bound<'_, PyAny>,
) -> PyResult<Vec<String>> {
    prose_window::ProseWindowChunker::new(budget(max_tokens)?, budget(overlap_tokens)?)
        .chunk(text, None)
        .map(|drafts| drafts.iter().map(draft_json).collect())
        .map_err(prose_error)
}

/// Raise the core `ChunkingError` with the crate's message.
///
/// The adapter only ever reaches this on the crate's own error paths.
fn chunking_error(py: Python<'_>, message: String) -> PyErr {
    domain_error(
        py,
        "research_engine.domain.errors",
        "ChunkingError",
        message,
    )
}

/// Build `module.class(message)` as the raised error. The accelerator does
/// not depend on `marginalia-ai`, so the class can be missing: then the
/// import (or attribute) error itself is raised, never a panic.
fn domain_error(py: Python<'_>, module: &str, class: &str, message: String) -> PyErr {
    PyModule::import(py, module)
        .and_then(|errors| errors.getattr(class))
        .and_then(|cls| cls.call1((message,)))
        .map_or_else(|err| err, PyErr::from_value)
}

/// Section-table chunking.
///
/// Mirrors `StructuralChunker.chunk` without the metadata copy (the adapter
/// rebuilds it from the original mapping plus the draft's own heading).
/// `sections_json` is one JSON array of section dicts.
///
/// # Errors
///
/// Returns `ValueError` when the table is not section JSON, and the core
/// `ChunkingError` on the crate's locate failures, with identical messages.
#[pyfunction]
#[pyo3(signature = (sections_json, full_text, max_tokens, overlap_tokens))]
fn chunk_structural(
    py: Python<'_>,
    sections_json: &str,
    full_text: Option<&str>,
    max_tokens: &Bound<'_, PyAny>,
    overlap_tokens: &Bound<'_, PyAny>,
) -> PyResult<Vec<String>> {
    let sections: Vec<structural::SectionInput> =
        serde_json::from_str(sections_json).map_err(|err| {
            PyValueError::new_err(format!(
                "marginalia_rs.chunk expects a section table, got error: {err}"
            ))
        })?;
    structural::StructuralChunker::new(budget(max_tokens)?, budget(overlap_tokens)?)
        .chunk(&sections, None, full_text)
        .map(|drafts| drafts.iter().map(draft_json).collect())
        .map_err(|err| structural_error(py, err))
}

/// Map a structural failure to its Python error.
///
/// Locate failures raise the core `ChunkingError` with the identical
/// message; a float used as a slice index raises `TypeError`, and an offset
/// `PassageDraft` would refuse raises `ValueError`, as upstream does.
fn structural_error(py: Python<'_>, err: Error) -> PyErr {
    match err {
        Error::Chunking(message) => chunking_error(py, message),
        Error::Type(message) => PyTypeError::new_err(message),
        Error::Validation(message) => PyValueError::new_err(message),
    }
}

pub fn register_chunkers(m: &Bound<'_, PyModule>) {
    for f in [
        wrap_pyfunction!(chunk_prose, m).expect("function name is a unique literal"),
        wrap_pyfunction!(chunk_structural, m).expect("function name is a unique literal"),
    ] {
        m.add_function(f).expect("module attribute assignment");
    }
}

#[cfg(test)]
mod tests {
    use super::{chunk_prose, chunk_structural, domain_error, prose_error, register_chunkers};
    use marginalia_chunk::{prose_window, structural};
    use pyo3::prelude::*;

    fn int(py: Python<'_>, value: i64) -> Bound<'_, PyAny> {
        value.into_pyobject(py).unwrap().into_any()
    }

    fn draft_jsons(json: &[String]) -> Vec<serde_json::Value> {
        json.iter()
            .map(|s| serde_json::from_str(s).unwrap())
            .collect()
    }

    #[test]
    fn prose_matches_crate_and_rejects_empty_budgets() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let text =
                "First sentence here. Second one follows! And a third? Yes indeed. ".repeat(40);
            let out = chunk_prose(&text, &int(py, 500), &int(py, 50)).unwrap();
            let expected = prose_window::ProseWindowChunker::new(500, 50)
                .chunk(&text, None)
                .unwrap();
            assert_eq!(out.len(), expected.len());
            for (got, want) in draft_jsons(&out).iter().zip(expected.iter()) {
                assert_eq!(got["char_start"], want.char_start);
                assert_eq!(got["char_end"], want.char_end);
                assert_eq!(got["text"], want.text);
                assert_eq!(got["token_count"], want.token_count.unwrap());
                assert_eq!(got["chunker_version"], "4.0");
            }
            assert!(chunk_prose("  ", &int(py, 500), &int(py, 50))
                .unwrap()
                .is_empty());
            let err = chunk_prose(&text, &int(py, 0), &int(py, 50)).unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            assert_eq!(err.value(py).to_string(), "max_tokens must be positive");
        });
    }

    const SECTIONS: &str = r#"[
        {"text": "Alpha section here. It fits.", "heading": "Alpha", "level": 1, "char_start": 0, "char_end": 29},
        {"text": "Beta text. ", "heading": "Beta", "level": 2, "page": 7, "char_start": 29, "char_end": 40}
    ]"#;

    #[test]
    fn structural_matches_crate_with_locators() {
        pyo3::prepare_freethreaded_python();
        let out = Python::with_gil(|py| {
            chunk_structural(py, SECTIONS, None, &int(py, 500), &int(py, 50)).unwrap()
        });
        let sections: Vec<structural::SectionInput> = serde_json::from_str(SECTIONS).unwrap();
        let expected = structural::StructuralChunker::new(500, 50)
            .chunk(&sections, None, None)
            .unwrap();
        assert_eq!(out.len(), expected.len());
        for (got, want) in draft_jsons(&out).iter().zip(expected.iter()) {
            assert_eq!(got["char_start"], want.char_start);
            assert_eq!(got["char_end"], want.char_end);
            assert_eq!(got["text"], want.text);
            assert_eq!(got["locator"], serde_json::to_value(&want.locator).unwrap());
            assert_eq!(got["chunker_version"], "4.0");
        }
    }

    #[test]
    fn a_missing_error_class_raises_instead_of_panicking() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let err = domain_error(py, "no_such_core_module", "ChunkingError", "m".into());
            assert!(err.is_instance_of::<pyo3::exceptions::PyModuleNotFoundError>(py));
            let err = domain_error(
                py,
                "research_engine.domain.errors",
                "NoSuchError",
                "m".into(),
            );
            assert!(err.is_instance_of::<pyo3::exceptions::PyAttributeError>(py));
        });
    }

    #[test]
    fn structural_surfaces_chunking_errors_as_chunking_errors() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            // Carried prose without offsets and no document to locate in.
            let bad = r#"[{"text": "Lost prose here", "heading": "Lost", "level": 1}]"#;
            let err = chunk_structural(py, bad, None, &int(py, 500), &int(py, 50)).unwrap_err();
            assert_eq!(
                err.get_type(py).name().unwrap().to_string(),
                "ChunkingError"
            );
            assert_eq!(
                err.value(py).to_string(),
                "Structural chunking needs offsets: supply full_text, or give each section char_start and char_end. Passages without a true span cannot be cited or re-anchored."
            );
            // Not a section table at all.
            assert!(chunk_structural(py, "[oops", None, &int(py, 500), &int(py, 50)).is_err());
            // Span reports text the document does not hold there.
            let mismatch = r#"[{"text": "Alpha", "char_start": 0, "char_end": 5}]"#;
            let err = chunk_structural(py, mismatch, Some("ZZZZZ"), &int(py, 500), &int(py, 50))
                .unwrap_err();
            assert_eq!(
                err.get_type(py).name().unwrap().to_string(),
                "ChunkingError"
            );
            assert_eq!(
                err.value(py).to_string(),
                "Section reports span (0, 5) but the text there does not match the section text."
            );
        });
    }

    #[test]
    fn registration_names_the_chunkers() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let m = PyModule::new(py, "chunk").unwrap();
            register_chunkers(&m);
            for name in ["chunk_prose", "chunk_structural"] {
                assert!(m.hasattr(name).unwrap(), "missing {name}");
            }
            for name in ["chunk_fixed", "chunk_whole"] {
                assert!(!m.hasattr(name).unwrap(), "{name} was cut back to Python");
            }
        });
    }

    #[test]
    fn foreign_variants_degrade_to_runtime_errors() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let err = prose_error(marginalia_types::Error::Chunking("boom".to_owned()));
            assert!(err.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py));
            assert!(err.value(py).to_string().contains("unexpected prose error"));
        });
    }

    #[test]
    fn budgets_past_i64_saturate_and_non_ints_refuse() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let huge = py.eval(c"2**63", None, None).unwrap();
            let tiny = py.eval(c"-2**63 - 1", None, None).unwrap();
            // Larger than any token count: one window, as upstream.
            let out = chunk_prose("abc. Def.", &huge, &huge).unwrap();
            assert_eq!(draft_jsons(&out)[0]["text"], "abc. Def.");
            // Smaller than any: refused as a non-positive budget.
            let err = chunk_prose("abc. Def.", &tiny, &int(py, 0)).unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            // Not an integer at all, in either position, on either chunker.
            let text = pyo3::types::PyString::new(py, "500").into_any();
            let n = int(py, 500);
            for (max, overlap) in [(&text, &n), (&n, &text)] {
                let err = chunk_prose("abc.", max, overlap).unwrap_err();
                assert!(err.is_instance_of::<pyo3::exceptions::PyTypeError>(py));
                let err = chunk_structural(py, SECTIONS, None, max, overlap).unwrap_err();
                assert!(err.is_instance_of::<pyo3::exceptions::PyTypeError>(py));
            }
        });
    }

    #[test]
    fn structural_offset_types_fail_as_python_does() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            // A float offset cannot slice the document: `TypeError`.
            let float = r#"[{"text": "Alpha", "char_start": 0.0, "char_end": 5}]"#;
            let err = chunk_structural(py, float, Some("Alpha"), &int(py, 500), &int(py, 50))
                .unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyTypeError>(py));
            // Without a document an integral float stands for its integer...
            let out = chunk_structural(py, float, None, &int(py, 500), &int(py, 50)).unwrap();
            assert_eq!(draft_jsons(&out)[0]["char_start"], 0);
            // ...and a fractional one is refused by the draft: `ValueError`.
            let frac = r#"[{"text": "Alpha", "char_start": 0.5, "char_end": 5}]"#;
            let err = chunk_structural(py, frac, None, &int(py, 500), &int(py, 50)).unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
        });
    }
}
