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
mod parse;
mod works;

/// Cargo profile this extension was compiled under, exposed as
/// `marginalia_rs.BUILD_PROFILE`. Plain `maturin build` is the dev profile
/// (opt-level 0, ~10x slower), so benchmarks refuse anything but `release`.
/// Selected by `cfg` rather than a runtime `if`: no branch, no coverage region.
#[cfg(debug_assertions)]
const BUILD_PROFILE: &str = "debug";
#[cfg(not(debug_assertions))]
const BUILD_PROFILE: &str = "release";

/// Fold away the differences that separate a quotation from its source.
///
/// Mirrors `services/text/normalize.py::normalize` exactly.
#[pyfunction]
fn normalize(text: &str) -> String {
    text_normalize::normalize(text)
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

/// The `marginalia_rs` extension root: one submodule per rewrite phase,
/// each holding only the seams the accelerator benchmark's gate kept.
#[pymodule]
fn marginalia_rs(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("BUILD_PROFILE", BUILD_PROFILE)
        .expect("module attribute assignment");
    m.add_submodule(&text_module(py))
        .expect("submodule name is unique");
    m.add_submodule(&chunk::chunk_module(py))
        .expect("submodule name is unique");
    m.add_submodule(&parse::parse_module(py))
        .expect("submodule name is unique");
    m.add_submodule(&works::works_module(py))
        .expect("submodule name is unique");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        marginalia_rs, normalize, normalize_for_matching, normalize_with_map, text_module,
        BUILD_PROFILE,
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
            assert_eq!(
                root.getattr("BUILD_PROFILE")
                    .unwrap()
                    .extract::<String>()
                    .unwrap(),
                BUILD_PROFILE,
            );
            let text = root.getattr("text").unwrap();
            assert_eq!(
                text.getattr("NORMALIZATION_VERSION")
                    .unwrap()
                    .extract::<String>()
                    .unwrap(),
                text_normalize::NORMALIZATION_VERSION,
            );
            let via_module: String = text
                .getattr("normalize")
                .unwrap()
                .call1(("hi",))
                .unwrap()
                .extract()
                .unwrap();
            assert_eq!(via_module, "hi");
            let _ = text_module(py);
            // Exactly the seams the benchmark gate kept: a binding re-added
            // without a Python caller (or a gate result) fails here.
            let public = |name: &str| -> Vec<String> {
                let mut names: Vec<String> = root
                    .getattr(name)
                    .unwrap()
                    .dir()
                    .unwrap()
                    .iter()
                    .map(|n| n.extract::<String>().unwrap())
                    .filter(|n| !n.starts_with('_'))
                    .collect();
                names.sort();
                names
            };
            assert_eq!(
                public("text"),
                [
                    "NORMALIZATION_VERSION",
                    "normalize",
                    "normalize_for_matching",
                    "normalize_with_map",
                ]
            );
            assert_eq!(public("chunk"), ["chunk_prose", "chunk_structural"]);
            assert_eq!(
                public("parse"),
                ["parse_epub", "parse_html", "parse_markdown"]
            );
            assert_eq!(public("works"), ["dominant_century"]);
        });
    }

    /// `cargo test` compiles the dev profile, so the constant must say so.
    #[cfg(debug_assertions)]
    #[test]
    fn test_builds_report_the_debug_profile() {
        assert_eq!(BUILD_PROFILE, "debug");
    }
}
