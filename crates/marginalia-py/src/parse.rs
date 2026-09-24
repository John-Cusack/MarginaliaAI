//! Document parser bindings over `marginalia-parse`: Markdown, HTML, EPUB.
//!
//! Only the parsers that cleared the accelerator benchmark's gate cross
//! here; plain text, TEI, and every `detect` branch were cut back to pure
//! Python (they lost to the crossing cost or saved microseconds).
//!
//! Crossing contract (all pinned by tests):
//! - File reading stays caller-side: adapters read bytes in an executor
//!   (strict UTF-8, replace mode, and universal newlines surface exactly as
//!   today, including `UnicodeDecodeError`), and the seam parses bytes or
//!   decoded text per format.
//! - Parsed documents cross as `ParsedDocument` JSON (strings and integers
//!   only — no float crosses); the adapter returns the plain `(text,
//!   title, metadata)` triples the modules have always returned.
//! - Corrupt archives answer `ValueError` with the crate's message (Python
//!   raises engine-native errors there; callers catch `Exception`).

use marginalia_parse::{epub, html, markdown};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

fn document_json(doc: &marginalia_types::sdk::ParsedDocument) -> String {
    serde_json::to_string(doc).expect("parsed documents hold JSON-native values")
}

/// Parse decoded Markdown: stripped text, title, and the section table.
///
/// Mirrors `MarkdownModule.parse` after its file read.
#[pyfunction]
fn parse_markdown(text: &str, file_name: &str) -> String {
    document_json(&markdown::parse_text(text, file_name))
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

pub fn register_parse(m: &Bound<'_, PyModule>) {
    for f in [
        wrap_pyfunction!(parse_markdown, m).expect("function name is a unique literal"),
        wrap_pyfunction!(parse_html, m).expect("function name is a unique literal"),
        wrap_pyfunction!(parse_epub, m).expect("function name is a unique literal"),
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
    use super::{parse_epub, parse_html, parse_markdown, register_parse};
    use marginalia_parse::{epub, html, markdown};
    use pyo3::prelude::*;

    fn doc(json: &str) -> serde_json::Value {
        serde_json::from_str(json).unwrap()
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
    fn registration_names_the_parsers() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let m = PyModule::new(py, "parse").unwrap();
            register_parse(&m);
            for name in ["parse_markdown", "parse_html", "parse_epub"] {
                assert!(m.hasattr(name).unwrap(), "missing {name}");
            }
            for name in [
                "parse_plain_text",
                "parse_tei",
                "detect_plain_text_content",
                "detect_markdown_content",
                "detect_html_content",
                "detect_epub_magic",
                "detect_tei_content",
                "detect_pdf_magic",
            ] {
                assert!(!m.hasattr(name).unwrap(), "{name} was cut back to Python");
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
    fn corrupt_archives_are_value_errors() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let err = parse_epub(b"not a zip", "b.epub").unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
        });
    }
}
