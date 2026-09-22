//! Document parser bindings over `marginalia-parse`.
//!
//! Crossing contract (all pinned by tests):
//! - File reading stays caller-side: adapters read bytes in an executor
//!   (strict UTF-8, replace mode, and universal newlines surface exactly as
//!   today, including `UnicodeDecodeError`), and the seam parses bytes or
//!   decoded text per format. Detection heads cross raw; the suffix and
//!   `mimetypes` branches stay caller-side (the OS table is not portable),
//!   ordered exactly as the modules order them.
//! - Parsed documents cross as `ParsedDocument` JSON (strings and integers
//!   only — no float crosses); the adapter returns the plain `(text,
//!   title, metadata)` triples the modules have always returned.
//! - Scores cross as binary `f64`, never decimals. Corrupt archives and
//!   markup answer `ValueError` with the crate's message (Python raises
//!   engine-native errors there; callers catch `Exception`).

use marginalia_parse::{epub, html, markdown, plain_text, tei};
use pyo3::exceptions::PyValueError;
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
        wrap_pyfunction!(parse_html, m).expect("function name is a unique literal"),
        wrap_pyfunction!(parse_epub, m).expect("function name is a unique literal"),
        wrap_pyfunction!(parse_tei, m).expect("function name is a unique literal"),
        wrap_pyfunction!(detect_html_content, m).expect("function name is a unique literal"),
        wrap_pyfunction!(detect_epub_magic, m).expect("function name is a unique literal"),
        wrap_pyfunction!(detect_tei_content, m).expect("function name is a unique literal"),
    ] {
        m.add_function(f).expect("module attribute assignment");
    }
}

pub fn parse_module(py: Python<'_>) -> Bound<'_, PyModule> {
    let m = PyModule::new(py, "parse").expect("module name is a valid literal");
    register_parse(&m);
    m
}

/// Parse raw HTML bytes: decoding (replace mode) plus extraction.
///
/// Mirrors `HTMLModule.parse` after its executor hop. Infallible in
/// practice — entity reconciliation resolves every shape — so this unwraps
/// rather than propagates (proven: no `Err` arm remains in the path).
#[pyfunction]
fn parse_html(raw: &[u8], file_name: &str) -> String {
    let doc = html::parse_bytes(raw, file_name).expect("html parsing is infallible");
    document_json(&doc)
}

/// Parse raw EPUB bytes: ZIP walk, spine order, section table.
///
/// Mirrors `EPUBModule.parse` after its executor hop.
///
/// # Errors
///
/// Returns `ValueError` with the crate's message on corrupt archives.
/// Python raises engine-native errors there (`BadZipFile` and kin);
/// callers catch `Exception`, so the contract is failure itself.
#[pyfunction]
fn parse_epub(raw: &[u8], file_name: &str) -> PyResult<String> {
    let doc =
        epub::parse_bytes(raw, file_name).map_err(|err| PyValueError::new_err(err.to_string()))?;
    Ok(document_json(&doc))
}

/// Parse raw TEI XML bytes: encoding declaration honored, failures raised.
///
/// Mirrors `TEIXMLModule.parse` after its executor hop.
///
/// # Errors
///
/// Returns `ValueError` with the crate's message on malformed XML, as above.
#[pyfunction]
fn parse_tei(raw: &[u8], file_name: &str) -> PyResult<String> {
    let doc =
        tei::parse_bytes(raw, file_name).map_err(|err| PyValueError::new_err(err.to_string()))?;
    Ok(document_json(&doc))
}

/// The marker-peek branch of HTML detection.
///
/// Answers `(0.7, …)` when the head holds `<html` or `<!doctype html>`,
/// `(0.0, …)` otherwise. Suffix and MIME stay caller-side, ordered ahead
/// exactly as the module orders them.
#[pyfunction]
fn detect_html_content(head: &str) -> (f64, String) {
    // Suffix-proof probe runs the content branch only.
    html::detect("probe", Some(head))
}

/// The ZIP-magic branch of EPUB detection.
///
/// Answers `(0.2, …)` for ZIP archives, `(0.0, …)` otherwise. Suffix and
/// MIME stay caller-side.
#[pyfunction]
fn detect_epub_magic(head: &[u8]) -> (f64, String) {
    epub::detect("probe", head)
}

/// The namespace-peek branch of TEI detection.
///
/// Answers `(0.95, …)` for the TEI namespace, `(0.7, …)` for a `<TEI`
/// root, `(0.0, …)` otherwise. The suffix gate stays caller-side.
#[pyfunction]
fn detect_tei_content(head: &str) -> (f64, String) {
    tei::detect("probe.xml", Some(head))
}
#[cfg(test)]
mod tests {
    use super::{
        detect_epub_magic, detect_html_content, detect_markdown_content, detect_plain_text_content,
        detect_tei_content, parse_epub, parse_html, parse_markdown, parse_plain_text, parse_tei,
        register_parse,
    };
    use marginalia_parse::{epub, html, markdown, plain_text, tei};
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
                "parse_html",
                "parse_epub",
                "parse_tei",
                "detect_html_content",
                "detect_epub_magic",
                "detect_tei_content",
            ] {
                assert!(m.hasattr(name).unwrap(), "missing {name}");
            }
        });
    }

    #[test]
    fn html_matches_crate_with_meta_and_sections() {
        let raw = b"<html><head><title>T</title><meta name=\"author\" content=\"A\">\
            </head><body><h1>H</h1><p>Body <a href=\"x\">link</a>.</p></body></html>";
        let got = doc(&parse_html(raw, "e.html"));
        let want = html::parse_bytes(raw, "e.html").unwrap();
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

    #[test]
    fn tei_matches_crate_with_header_counts() {
        let raw = b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\
            <TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><teiHeader><titleStmt>\
            <title>Work</title><author>Auth</author></titleStmt></teiHeader>\
            <text><body><div><p>Prose here.</p></div></body></text></TEI>";
        let got = doc(&parse_tei(raw, "w.xml").unwrap());
        let want = tei::parse_bytes(raw, "w.xml").unwrap();
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

    #[test]
    fn epub_matches_crate_on_minimal_book() {
        use std::io::Write;
        // Minimal book mirroring the crate's own fixture shapes.
        let container = "<?xml version=\"1.0\"?><container \
             xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">\
             <rootfiles><rootfile media-type=\"application/oebps-package+xml\" \
             full-path=\"EPUB/content.opf\"/></rootfiles></container>";
        let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
            <metadata></metadata><manifest>\
            <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
            </manifest><spine><itemref idref=\"c1\"/></spine></package>";
        let chapter = b"<html><body><h1>H</h1><p>B</p></body></html>";
        let mut buf = Vec::new();
        {
            let mut zip = zip::ZipWriter::new(std::io::Cursor::new(&mut buf));
            let opts = zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Stored);
            for (name, content) in [
                ("META-INF/container.xml", container.as_bytes()),
                ("EPUB/content.opf", opf.as_bytes()),
                ("EPUB/c1.xhtml", chapter.as_slice()),
            ] {
                zip.start_file(name, opts).unwrap();
                zip.write_all(content).unwrap();
            }
            zip.finish().unwrap();
        }
        let got = doc(&parse_epub(&buf, "t.epub").unwrap());
        let want = epub::parse_bytes(&buf, "t.epub").unwrap();
        assert_eq!(got["text"], want.text);
        assert_eq!(
            got["metadata"],
            serde_json::to_value(&want.metadata).unwrap()
        );
    }

    #[test]
    fn corrupt_archives_and_markup_are_value_errors() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let err = parse_epub(b"not a zip", "b.epub").unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            let err = parse_tei(b"<TEI><text><p>x</q>", "b.xml").unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
        });
    }

    #[test]
    fn detect_branches_match_crate() {
        assert_eq!(
            detect_html_content("<HTML><BODY>"),
            (0.7, "file contains HTML markers".to_owned())
        );
        assert_eq!(
            detect_html_content("plain"),
            (0.0, "not detected as HTML".to_owned())
        );
        assert_eq!(
            detect_epub_magic(b"PK\x03\x04rest"),
            (0.2, "file is a ZIP archive (could be EPUB)".to_owned())
        );
        assert_eq!(
            detect_epub_magic(b"nope"),
            (0.0, "not detected as EPUB".to_owned())
        );
        assert_eq!(
            detect_tei_content("<TEI xmlns=\"http://www.tei-c.org/ns/1.0\">"),
            (0.95, "file contains TEI namespace declaration".to_owned())
        );
        assert_eq!(
            detect_tei_content("<TEI>"),
            (0.7, "file contains <TEI> root element".to_owned())
        );
        assert_eq!(
            detect_tei_content("plain"),
            (0.0, "not detected as TEI XML".to_owned())
        );
    }
}
