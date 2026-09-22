//! Plain-text ingestion, mirroring `modules/plain_text.py`.
//!
//! The module reads strict UTF-8 (`Path.read_text(encoding="utf-8")` — a bad
//! byte raises rather than substituting), titles from the first non-empty
//! line of at most 200 characters, and counts lines with `str.splitlines()`.

use marginalia_types::sdk::ParsedDocument;
use marginalia_types::Result;
use serde_json::{Map, Value};

use crate::pystr::{line_count, py_stem, splitlines};
use crate::{decode_strict, lower_suffix};

pub const MODULE_ID: &str = "plain_text";
pub const MODULE_VERSION: &str = "1.0";
pub const DEFAULT_CHUNKER: &str = "prose_window";
pub const DEFAULT_DOCUMENT_TYPE: &str = "generic";

/// `detect`: the extension branch, then the UTF-8 fallback. The `mimetypes`
/// branch between them reads the OS table and stays caller-side.
pub fn detect(file_name: &str, head: Option<&[u8]>) -> (f64, String) {
    let suffix = lower_suffix(file_name);
    if suffix == ".txt" {
        return (0.8, format!("extension '{suffix}' matches plain text"));
    }
    // The head is read strict, the way the module opens it: undecodable
    // bytes fail the fallback instead of scoring.
    if head.is_some_and(|raw| std::str::from_utf8(raw).is_ok()) {
        return (
            0.3,
            "file is valid UTF-8 text (fallback detection)".to_owned(),
        );
    }
    (0.0, "not detected as plain text".to_owned())
}

/// Parse strict-UTF-8 bytes. A bad byte is a parse failure, mirroring the
/// `UnicodeDecodeError` the module lets through.
pub fn parse_bytes(raw: &[u8], file_name: &str) -> Result<ParsedDocument> {
    // The module reads in text mode: universal newlines reach the parser.
    Ok(parse_text(
        &crate::translate_newlines(decode_strict(raw)?),
        file_name,
    ))
}

/// Parse already-decoded text: title plus the three metadata keys.
pub fn parse_text(text: &str, file_name: &str) -> ParsedDocument {
    let stem = py_stem(file_name);
    let mut title = stem.to_owned();
    for line in splitlines(text) {
        let stripped = marginalia_text::chars::strip(line);
        if !stripped.is_empty() && stripped.chars().count() <= 200 {
            title = stripped.to_owned();
            break;
        }
    }
    let mut metadata = Map::new();
    metadata.insert(
        "char_count".to_owned(),
        Value::from(text.chars().count() as u64),
    );
    metadata.insert(
        "line_count".to_owned(),
        Value::from(line_count(text) as u64),
    );
    metadata.insert("file_name".to_owned(), Value::from(file_name));
    ParsedDocument {
        title: Some(title),
        text: text.to_owned(),
        document_type: DEFAULT_DOCUMENT_TYPE.to_owned(),
        language: None,
        metadata,
        sections: Vec::new(),
        structural_locators: Vec::new(),
    }
}
