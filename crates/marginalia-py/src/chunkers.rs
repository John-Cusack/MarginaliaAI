//! Text/section chunker bindings over `marginalia-chunk`.
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
//!   the object through.
//! - `chunk_structural` raises the real `ChunkingError` with the crate's
//!   message; `chunk_prose` re-raises `cap_spans` rejections as `ValueError`
//!   with the identical text. Both error paths are fired by tests.

use marginalia_chunk::{fixed_window, prose_window, structural, whole_or_paragraph};
use marginalia_types::sdk::PassageDraft;
use marginalia_types::Error;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

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

/// Fixed windows over `text`.
///
/// Mirrors `FixedWindowChunker.chunk` without the metadata passthrough
/// (the adapter reattaches the original mapping).
#[pyfunction]
#[pyo3(signature = (text, window_chars, overlap_chars))]
fn chunk_fixed(text: &str, window_chars: i64, overlap_chars: i64) -> Vec<String> {
    fixed_window::FixedWindowChunker::new(window_chars, overlap_chars)
        .chunk(text, None)
        .iter()
        .map(draft_json)
        .collect()
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
fn chunk_prose(text: &str, max_tokens: i64, overlap_tokens: i64) -> PyResult<Vec<String>> {
    prose_window::ProseWindowChunker::new(max_tokens, overlap_tokens)
        .chunk(text, None)
        .map(|drafts| drafts.iter().map(draft_json).collect())
        .map_err(prose_error)
}

/// Whole-or-paragraph windows over `text`.
///
/// Mirrors `WholeOrParagraphChunker.chunk` without the metadata passthrough.
#[pyfunction]
#[pyo3(signature = (text, threshold_tokens))]
fn chunk_whole(text: &str, threshold_tokens: i64) -> Vec<String> {
    whole_or_paragraph::WholeOrParagraphChunker::new(threshold_tokens)
        .chunk(text, None)
        // Proven infallible: no `Err` arm exists in the chunker.
        .expect("whole chunking is infallible")
        .iter()
        .map(draft_json)
        .collect()
}

/// Raise the core `ChunkingError` with the crate's message.
///
/// The adapter only ever reaches this on the crate's own error paths.
fn chunking_error(py: Python<'_>, message: String) -> PyErr {
    let errors = PyModule::import(py, "research_engine.domain.errors")
        .expect("core error module is importable");
    let cls = errors
        .getattr("ChunkingError")
        .expect("ChunkingError is defined");
    PyErr::from_value(cls.call1((message,)).expect("exception construction"))
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
    max_tokens: i64,
    overlap_tokens: i64,
) -> PyResult<Vec<String>> {
    let sections: Vec<structural::SectionInput> =
        serde_json::from_str(sections_json).map_err(|err| {
            PyValueError::new_err(format!(
                "marginalia_rs.chunk expects a section table, got error: {err}"
            ))
        })?;
    structural::StructuralChunker::new(max_tokens, overlap_tokens)
        .chunk(&sections, None, full_text)
        .map(|drafts| drafts.iter().map(draft_json).collect())
        .map_err(|err| structural_error(py, err))
}

/// Map a structural failure to its Python error.
///
/// Locate failures raise the core `ChunkingError` with the identical
/// message; anything else degrades to `RuntimeError`, as above.
fn structural_error(py: Python<'_>, err: Error) -> PyErr {
    match err {
        Error::Chunking(message) => chunking_error(py, message),
        other => PyRuntimeError::new_err(format!("unexpected structural error: {other:?}")),
    }
}

pub fn register_chunkers(m: &Bound<'_, PyModule>) {
    for f in [
        wrap_pyfunction!(chunk_fixed, m).expect("function name is a unique literal"),
        wrap_pyfunction!(chunk_prose, m).expect("function name is a unique literal"),
        wrap_pyfunction!(chunk_whole, m).expect("function name is a unique literal"),
        wrap_pyfunction!(chunk_structural, m).expect("function name is a unique literal"),
    ] {
        m.add_function(f).expect("module attribute assignment");
    }
}

#[cfg(test)]
mod tests {
    use super::{
        chunk_fixed, chunk_prose, chunk_structural, chunk_whole, prose_error, register_chunkers,
        structural_error,
    };
    use marginalia_chunk::{fixed_window, prose_window, structural, whole_or_paragraph};
    use pyo3::prelude::*;

    fn draft_jsons(json: &[String]) -> Vec<serde_json::Value> {
        json.iter()
            .map(|s| serde_json::from_str(s).unwrap())
            .collect()
    }

    #[test]
    fn fixed_matches_crate_draft_for_draft() {
        let text = "Word ".repeat(3000);
        let out = chunk_fixed(&text, 2000, 200);
        let expected = fixed_window::FixedWindowChunker::new(2000, 200).chunk(&text, None);
        assert_eq!(out.len(), expected.len());
        for (got, want) in draft_jsons(&out).iter().zip(expected.iter()) {
            assert_eq!(got["char_start"], want.char_start);
            assert_eq!(got["char_end"], want.char_end);
            assert_eq!(got["text"], want.text);
            assert_eq!(got["token_count"], want.token_count.unwrap());
            assert_eq!(got["chunker"], "fixed_window");
            assert_eq!(got["chunker_version"], "3.0");
        }
        assert!(chunk_fixed("   \n ", 2000, 200).is_empty());
        assert!(chunk_fixed("", 2000, 200).is_empty());
    }

    #[test]
    fn prose_matches_crate_and_rejects_empty_budgets() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let text =
                "First sentence here. Second one follows! And a third? Yes indeed. ".repeat(40);
            let out = chunk_prose(&text, 500, 50).unwrap();
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
            assert!(chunk_prose("  ", 500, 50).unwrap().is_empty());
            let err = chunk_prose(&text, 0, 50).unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            assert_eq!(err.value(py).to_string(), "max_tokens must be positive");
        });
    }

    #[test]
    fn whole_matches_crate_on_short_and_long_docs() {
        let short = "A short doc.";
        let out = chunk_whole(short, 600);
        assert_eq!(out.len(), 1);
        let long = ("Para one.\n\n".to_owned() + &"x".repeat(60_000)).repeat(2);
        let out = chunk_whole(&long, 600);
        let expected = whole_or_paragraph::WholeOrParagraphChunker::new(600)
            .chunk(&long, None)
            .expect("whole chunking is infallible");
        assert_eq!(out.len(), expected.len());
        for (got, want) in draft_jsons(&out).iter().zip(expected.iter()) {
            assert_eq!(got["char_start"], want.char_start);
            assert_eq!(got["text"], want.text);
            assert_eq!(got["chunker"], "whole_or_paragraph");
        }
        assert!(chunk_whole(" \n", 600).is_empty());
    }

    const SECTIONS: &str = r#"[
        {"text": "Alpha section here. It fits.", "heading": "Alpha", "level": 1, "char_start": 0, "char_end": 29},
        {"text": "Beta text. ", "heading": "Beta", "level": 2, "page": 7, "char_start": 29, "char_end": 40}
    ]"#;

    #[test]
    fn structural_matches_crate_with_locators() {
        pyo3::prepare_freethreaded_python();
        let out = Python::with_gil(|py| chunk_structural(py, SECTIONS, None, 500, 50).unwrap());
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
    fn structural_surfaces_chunking_errors_as_chunking_errors() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            // Carried prose without offsets and no document to locate in.
            let bad = r#"[{"text": "Lost prose here", "heading": "Lost", "level": 1}]"#;
            let err = chunk_structural(py, bad, None, 500, 50).unwrap_err();
            assert_eq!(
                err.get_type(py).name().unwrap().to_string(),
                "ChunkingError"
            );
            assert_eq!(
                err.value(py).to_string(),
                "Structural chunking needs offsets: supply full_text, or give each section char_start and char_end. Passages without a true span cannot be cited or re-anchored."
            );
            // Not a section table at all.
            assert!(chunk_structural(py, "[oops", None, 500, 50).is_err());
            // Span reports text the document does not hold there.
            let mismatch = r#"[{"text": "Alpha", "char_start": 0, "char_end": 5}]"#;
            let err = chunk_structural(py, mismatch, Some("ZZZZZ"), 500, 50).unwrap_err();
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
            for name in [
                "chunk_fixed",
                "chunk_prose",
                "chunk_whole",
                "chunk_structural",
            ] {
                assert!(m.hasattr(name).unwrap(), "missing {name}");
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
            let err = structural_error(py, marginalia_types::Error::Validation("boom".to_owned()));
            assert!(err.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py));
            assert!(err
                .value(py)
                .to_string()
                .contains("unexpected structural error"));
        });
    }
}
