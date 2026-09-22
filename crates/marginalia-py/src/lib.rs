//! PyO3 seam: `marginalia_rs.*` — signature-identical wrappers over the
//! phase crates. All behavior lives in the wrapped crates (proven by their
//! differentials); this crate adds no logic, only binding plumbing.
//!
//! The `extension-module` feature is enabled by maturin wheel builds.
//! Plain `cargo test` builds without it, so unit tests link as an
//! ordinary rlib and still execute every wrapper body.

use marginalia_text::normalize as text_normalize;
use pyo3::prelude::*;

mod chunk;
mod chunkers;
mod langconfig;
mod parse;
mod windows;

/// Fold away the differences that separate a quotation from its source.
///
/// Mirrors `services/text/normalize.py::normalize` exactly.
#[pyfunction]
fn normalize(text: &str) -> String {
    text_normalize::normalize(text)
}

/// Collapse whitespace runs only.
///
/// Mirrors `services/text/normalize.py::normalize_whitespace` exactly.
#[pyfunction]
fn normalize_whitespace(text: &str) -> String {
    text_normalize::normalize_whitespace(text)
}

/// `normalize`, plus a map from each output character to its raw offset.
///
/// `index_map[i]` is the character offset in `text` of `out[i]`.
/// Mirrors `services/text/normalize.py::normalize_with_map` exactly.
#[pyfunction]
fn normalize_with_map(text: &str) -> (String, Vec<usize>) {
    text_normalize::normalize_with_map(text)
}

/// The query-side counterpart of `normalize_with_map`.
///
/// Mirrors `services/text/normalize.py::normalize_for_matching` exactly.
#[pyfunction]
fn normalize_for_matching(text: &str) -> String {
    text_normalize::normalize_for_matching(text)
}

fn text_module(py: Python<'_>) -> Bound<'_, PyModule> {
    let m = PyModule::new(py, "text").expect("module name is a valid literal");
    for f in [
        wrap_pyfunction!(normalize, &m),
        wrap_pyfunction!(normalize_whitespace, &m),
        wrap_pyfunction!(normalize_with_map, &m),
        wrap_pyfunction!(normalize_for_matching, &m),
    ] {
        m.add_function(f.expect("function names are unique literals"))
            .expect("module attribute assignment");
    }
    m.add(
        "NORMALIZATION_VERSION",
        text_normalize::NORMALIZATION_VERSION,
    )
    .expect("module attribute assignment");
    m
}

/// The `marginalia_rs` extension root: one submodule per rewrite phase.
#[pymodule]
fn marginalia_rs(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_submodule(&text_module(py))
        .expect("submodule name is unique");
    m.add_submodule(&chunk::chunk_module(py))
        .expect("submodule name is unique");
    m.add_submodule(&parse::parse_module(py))
        .expect("submodule name is unique");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        marginalia_rs, normalize, normalize_for_matching, normalize_whitespace, normalize_with_map,
        text_module,
    };
    use marginalia_text::normalize as text_normalize;
    use pyo3::prelude::*;

    #[test]
    fn wrappers_match_python_signatures_on_representative_inputs() {
        let cases = [
            "",
            "  plain ascii  ",
            "fis-\ncal",
            "Anglo-\nSaxon",
            "\u{201c}quoted\u{201d} \u{2014} dash",
            "decomposed e\u{301} vs \u{e9}",
            "  \u{1c}control  spaces\u{3000}here  ",
        ];
        for text in cases {
            assert_eq!(normalize(text), text_normalize::normalize(text));
            assert_eq!(
                normalize_whitespace(text),
                text_normalize::normalize_whitespace(text)
            );
            assert_eq!(
                normalize_with_map(text),
                text_normalize::normalize_with_map(text)
            );
            assert_eq!(
                normalize_for_matching(text),
                text_normalize::normalize_for_matching(text)
            );
        }
    }

    #[test]
    fn module_tree_registers_text_with_version() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let root = PyModule::new(py, "marginalia_rs").unwrap();
            marginalia_rs(py, &root).unwrap();
            let text = root.getattr("text").unwrap();
            assert_eq!(
                text.getattr("NORMALIZATION_VERSION")
                    .unwrap()
                    .extract::<String>()
                    .unwrap(),
                text_normalize::NORMALIZATION_VERSION,
            );
            for name in [
                "normalize",
                "normalize_whitespace",
                "normalize_with_map",
                "normalize_for_matching",
            ] {
                assert!(text.hasattr(name).unwrap(), "missing {name}");
            }
            let via_module: String = text
                .getattr("normalize")
                .unwrap()
                .call1(("hi",))
                .unwrap()
                .extract()
                .unwrap();
            assert_eq!(via_module, "hi");
            let _ = text_module(py);
            let chunk = root.getattr("chunk").unwrap();
            assert_eq!(
                chunk.getattr("RRF_K").unwrap().extract::<f64>().unwrap(),
                60.0
            );
            for name in ["rrf_fuse", "weighted_fuse"] {
                assert!(chunk.hasattr(name).unwrap(), "missing {name}");
            }
            let parse = root.getattr("parse").unwrap();
            for name in [
                "parse_plain_text",
                "parse_markdown",
                "detect_plain_text_content",
                "detect_markdown_content",
            ] {
                assert!(parse.hasattr(name).unwrap(), "missing {name}");
            }
        });
    }
}
