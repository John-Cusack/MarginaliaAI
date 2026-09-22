//! Plain-text and Markdown parser bindings over `marginalia-parse`.
//!
//! Crossing contract (all pinned by tests):
//! - File reading and decoding stay caller-side: the adapter reads bytes in
//!   an executor (strict UTF-8 and universal newlines surface exactly as
//!   today, including `UnicodeDecodeError`), and the seam parses decoded
//!   text. Detection heads cross as raw bytes; the suffix and `mimetypes`
//!   branches stay caller-side (the OS table is not portable), ordered
//!   exactly as the modules order them.
//! - Parsed documents cross as `ParsedDocument` JSON. Titles, texts, counts,
//!   and section tables are strings and integers — no float crosses — and
//!   the adapter returns the plain `(text, title, metadata)` triples the
//!   modules have always returned.
//! - Scores cross as binary `f64`, never decimals.

use marginalia_parse::{markdown, plain_text};
use pyo3::prelude::*;

fn document_json(doc: &marginalia_types::sdk::ParsedDocument) -> String {
    serde_json::to_string(doc).expect("parsed documents hold JSON-native values")
}

/// Parse decoded plain text: title plus the three metadata keys.
///
/// Mirrors `PlainTextModule.parse` after its file read.
#[pyfunction]
fn parse_plain_text(text: &str, file_name: &str) -> String {
    document_json(&plain_text::parse_text(text, file_name))
}

/// Parse decoded Markdown: stripped text, title, and the section table.
///
/// Mirrors `MarkdownModule.parse` after its file read.
#[pyfunction]
fn parse_markdown(text: &str, file_name: &str) -> String {
    document_json(&markdown::parse_text(text, file_name))
}

/// The content-fallback branch of plain-text detection.
///
/// Answers `(0.3, …)` for valid UTF-8 heads, `(0.0, …)` otherwise.
/// Mirrors the tail of `PlainTextModule.detect`; suffix and MIME stay
/// caller-side, ordered ahead of this exactly as the module orders them.
#[pyfunction]
fn detect_plain_text_content(head: &[u8]) -> (f64, String) {
    // The crate's `detect` with a suffix-proof name runs the content branch
    // only: no real file stem is extensionless, and this probe is.
    plain_text::detect("plain_text_probe", Some(head))
}

/// The heading-peek branch of Markdown detection.
///
/// Answers `(0.4, …)` when the head holds a Markdown heading, `(0.0, …)`
/// otherwise (including undecodable heads). Mirrors the tail of
/// `MarkdownModule.detect`; suffix and MIME stay caller-side.
#[pyfunction]
fn detect_markdown_content(head: &[u8]) -> (f64, String) {
    markdown::detect("markdown_probe", Some(head))
}

pub fn register_parse(m: &Bound<'_, PyModule>) {
    for f in [
        wrap_pyfunction!(parse_plain_text, m).expect("function name is a unique literal"),
        wrap_pyfunction!(parse_markdown, m).expect("function name is a unique literal"),
        wrap_pyfunction!(detect_plain_text_content, m).expect("function name is a unique literal"),
        wrap_pyfunction!(detect_markdown_content, m).expect("function name is a unique literal"),
    ] {
        m.add_function(f).expect("module attribute assignment");
    }
}

pub fn parse_module(py: Python<'_>) -> Bound<'_, PyModule> {
    let m = PyModule::new(py, "parse").expect("module name is a valid literal");
    register_parse(&m);
    m
}

#[cfg(test)]
mod tests {
    use super::{
        detect_markdown_content, detect_plain_text_content, parse_markdown, parse_plain_text,
        register_parse,
    };
    use marginalia_parse::{markdown, plain_text};
    use pyo3::prelude::*;

    fn doc(json: &str) -> serde_json::Value {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn plain_text_matches_crate_on_prose_and_edges() {
        for (text, name) in [
            ("Title here\n\nBody text.\n", "doc.txt"),
            ("", "empty.txt"),
            ("   \n\t  \n", "blank.txt"),
            ("a\n".repeat(500).as_str(), "long.txt"),
            ("ünïcodé tître\n\nBodÿ with émojis 😀.\n", "uni.txt"),
        ] {
            let got = doc(&parse_plain_text(text, name));
            let want = plain_text::parse_text(text, name);
            assert_eq!(got["text"], want.text);
            assert_eq!(
                got["title"].as_str().unwrap(),
                want.title.as_deref().unwrap()
            );
            assert_eq!(
                got["metadata"],
                serde_json::to_value(&want.metadata).unwrap()
            );
        }
    }

    #[test]
    fn markdown_matches_crate_with_sections() {
        let text = "# The Whole Thing\n\nOpening prose.\n\n## Part One\n\nBody **bold** and a [link](https://example.com).\n";
        let got = doc(&parse_markdown(text, "book.md"));
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
    fn detect_content_branches_match_crate() {
        assert_eq!(
            detect_plain_text_content(b"hello"),
            (
                0.3,
                "file is valid UTF-8 text (fallback detection)".to_owned()
            )
        );
        assert_eq!(
            detect_plain_text_content(b"\xff\xfe"),
            (0.0, "not detected as plain text".to_owned())
        );
        assert_eq!(
            detect_markdown_content(b"# Heading\n"),
            (0.4, "file contains markdown headings".to_owned())
        );
        assert_eq!(
            detect_markdown_content(b"plain prose"),
            (0.0, "not detected as markdown".to_owned())
        );
        assert_eq!(
            detect_markdown_content(b"\xff"),
            (0.0, "not detected as markdown".to_owned())
        );
    }

    #[test]
    fn registration_names_the_parsers() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let m = PyModule::new(py, "parse").unwrap();
            register_parse(&m);
            for name in [
                "parse_plain_text",
                "parse_markdown",
                "detect_plain_text_content",
                "detect_markdown_content",
            ] {
                assert!(m.hasattr(name).unwrap(), "missing {name}");
            }
        });
    }
}
