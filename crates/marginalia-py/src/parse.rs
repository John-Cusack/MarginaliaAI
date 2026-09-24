//! Document parser binding over `marginalia-parse`: Markdown only.
//!
//! HTML and EPUB parsing cleared the benchmark but not the review (stack
//! overflow on deep nesting, OPF entity double-decoding, tree-builder
//! divergence from `html.parser`), so they stay pure Python, as do plain
//! text, TEI, and every `detect` branch.
//!
//! Crossing contract (pinned by tests): the file read stays caller-side
//! (strict UTF-8 and universal newlines surface exactly as today, including
//! `UnicodeDecodeError`); the parsed document crosses as `ParsedDocument`
//! JSON (strings and integers only), and the adapter returns the plain
//! `(text, title, metadata)` triple the module has always returned.

use marginalia_parse::markdown;
use pyo3::prelude::*;

/// Parse decoded Markdown: stripped text, title, and the section table.
///
/// Mirrors `MarkdownModule.parse` after its file read.
#[pyfunction]
fn parse_markdown(text: &str, file_name: &str) -> String {
    serde_json::to_string(&markdown::parse_text(text, file_name))
        .expect("parsed documents hold JSON-native values")
}

pub fn parse_module(py: Python<'_>) -> Bound<'_, PyModule> {
    let m = PyModule::new(py, "parse").expect("module name is a valid literal");
    m.add_function(
        wrap_pyfunction!(parse_markdown, &m).expect("function name is a unique literal"),
    )
    .expect("module attribute assignment");
    m
}

#[cfg(test)]
mod tests {
    use super::{parse_markdown, parse_module};
    use marginalia_parse::markdown;
    use pyo3::prelude::*;

    #[test]
    fn markdown_matches_crate_with_sections() {
        let text = "# The Whole Thing\n\nOpening prose.\n\n## Part One\n\nBody **bold** and a [link](https://example.com).\n";
        let got: serde_json::Value =
            serde_json::from_str(&parse_markdown(text, "book.md")).unwrap();
        let want = markdown::parse_text(text, "book.md");
        assert_eq!(got["text"], want.text);
        assert_eq!(
            got["title"].as_str().unwrap(),
            want.title.as_deref().unwrap()
        );
        assert_eq!(
            got["metadata"],
            serde_json::to_value(&want.metadata).unwrap()
        );
        // The table travels in the `sections` field, not duplicated under
        // metadata: the adapter maps it back for the module triple.
        assert_eq!(
            got["sections"],
            serde_json::to_value(&want.sections).unwrap()
        );
    }

    #[test]
    fn registration_names_only_markdown() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let m = parse_module(py);
            assert!(m.hasattr("parse_markdown").unwrap());
            for name in ["parse_html", "parse_epub", "parse_tei", "parse_plain_text"] {
                assert!(!m.hasattr(name).unwrap(), "{name} stays pure Python");
            }
        });
    }
}
