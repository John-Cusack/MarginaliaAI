//! Works authorship bindings over `marginalia-works`.
//!
//! Crossing contract (all pinned by tests):
//! - Hash rows cross as JSON strings (dumped adapter-side with plain
//!   `json.dumps` — callers pre-stringify ids, exactly as the Python path
//!   consumes them). Floats round-trip bit-exactly through the workspace's
//!   `float_roundtrip` serde flag; non-finite floats have no JSON spelling
//!   and are outside the contract. Digests cross as bytes.
//! - Markers cross as plain strings. `format_marker` takes the key in its
//!   canonical string form; anything unparseable answers `ValueError`.
//! - Missing row keys read `null` in the crate where Python raises
//!   `KeyError`: rows are complete by construction (DB-fed), and malformed
//!   rows are a typed-boundary deviation, pinned.

use marginalia_works::{hashing, markers};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use uuid::Uuid;

/// Hash authored content: blocks in tree order, citations and links sorted
/// by their identity columns. Each argument is one JSON array of row maps.
///
/// Mirrors `services/works/hashing.py::compute_content_hash` exactly.
///
/// # Errors
///
#[pyfunction]
fn compute_content_hash<'py>(
    py: Python<'py>,
    blocks_json: &str,
    citations_json: &str,
    source_links_json: &str,
    entity_links_json: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    fn parse_table(json: &str) -> PyResult<Vec<serde_json::Map<String, serde_json::Value>>> {
        serde_json::from_str(json).map_err(|err| {
            PyValueError::new_err(format!(
                "marginalia_rs.works expects row JSON, got error: {err}"
            ))
        })
    }
    let digest = hashing::compute_content_hash(
        &parse_table(blocks_json)?,
        &parse_table(citations_json)?,
        &parse_table(source_links_json)?,
        &parse_table(entity_links_json)?,
    );
    Ok(PyBytes::new(py, &digest))
}

/// Split a block's markers into valid citation-key strings and dangling raw
/// text. Mirrors `services/works/markers.py::find_markers` exactly.
#[pyfunction]
fn find_markers(text: &str) -> (std::collections::HashSet<String>, Vec<String>) {
    markers::find_markers(text)
}

/// The marker text for an occurrence, from its canonical key string.
///
/// Mirrors `services/works/markers.py::format_marker` exactly.
///
/// # Errors
///
/// Returns `ValueError` when the key is not a UUID.
#[pyfunction]
fn format_marker(citation_key: &str) -> PyResult<String> {
    let key = Uuid::parse_str(citation_key).map_err(|_| {
        PyValueError::new_err(format!(
            "marginalia_rs.works expects UUID keys, got {citation_key:?}"
        ))
    })?;
    Ok(markers::format_marker(&key))
}

pub fn works_module(py: Python<'_>) -> Bound<'_, PyModule> {
    let m = PyModule::new(py, "works").expect("module name is a valid literal");
    register_works(&m);
    m
}

pub fn register_works(m: &Bound<'_, PyModule>) {
    for f in [
        wrap_pyfunction!(compute_content_hash, m).expect("function name is a unique literal"),
        wrap_pyfunction!(find_markers, m).expect("function name is a unique literal"),
        wrap_pyfunction!(format_marker, m).expect("function name is a unique literal"),
    ] {
        m.add_function(f).expect("module attribute assignment");
    }
}

#[cfg(test)]
mod tests {
    use super::{compute_content_hash, find_markers, format_marker, register_works};
    use pyo3::prelude::*;
    use pyo3::types::PyBytes;

    fn rows(json: &str) -> Vec<serde_json::Map<String, serde_json::Value>> {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn hash_matches_crate_on_shaped_rows() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let blocks = r#"[{"block_key": "b1", "parent_key": null, "position": 0,
                "block_type": "prose", "title": "T", "body_markdown": "Hello."}]"#;
            let citations = r#"[{"block_key": "b1", "citation_key": "c1", "position": 0,
                "intent": "supports", "placement": "inline", "edition_id": null,
                "edition_key": null, "source_span_id": null, "quoted_text": "Hi",
                "verify_status": "unverified", "locator": {"page": 3},
                "prefix": "", "suffix": "", "suppress_author": false}]"#;
            let links = r#"[{"block_key": "b1", "source_span_id": "s1",
                "relation": "cites", "confidence": 0.75, "note": ""}]"#;
            let elinks = r#"[{"block_key": "b1", "entity_id": "e1",
                "relation": "mentions", "surface_form": "X"}]"#;
            let out: Bound<'_, PyBytes> =
                compute_content_hash(py, blocks, citations, links, elinks).unwrap();
            let expected = marginalia_works::hashing::compute_content_hash(
                &rows(blocks),
                &rows(citations),
                &rows(links),
                &rows(elinks),
            );
            assert_eq!(out.as_bytes(), expected);
            // Empty tables hash deterministically too.
            let empty = compute_content_hash(py, "[]", "[]", "[]", "[]").unwrap();
            assert_eq!(empty.as_bytes().len(), 32);
        });
    }

    #[test]
    fn hash_rejects_malformed_tables() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            assert!(compute_content_hash(py, "[oops", "[]", "[]", "[]").is_err());
            assert!(compute_content_hash(py, "[]", "{}", "[]", "[]").is_err());
            assert!(compute_content_hash(py, "[]", "[]", "[oops", "[]").is_err());
            assert!(compute_content_hash(py, "[]", "[]", "[]", "{}").is_err());
        });
    }

    #[test]
    fn markers_match_python_shapes() {
        let (keys, invalid) =
            find_markers("a {{cite:12345678-1234-5678-1234-567812345678}} b {{cite:nope}}");
        assert!(keys.contains("12345678-1234-5678-1234-567812345678"));
        assert_eq!(invalid, vec!["{{cite:nope}}"]);
        assert_eq!(
            format_marker("12345678-1234-5678-1234-567812345678").unwrap(),
            "{{cite:12345678-1234-5678-1234-567812345678}}"
        );
        assert!(format_marker("nope").is_err());
    }

    #[test]
    fn registration_names_the_works() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let m = PyModule::new(py, "works").unwrap();
            register_works(&m);
            for name in ["compute_content_hash", "find_markers", "format_marker"] {
                assert!(m.hasattr(name).unwrap(), "missing {name}");
            }
        });
    }
}
