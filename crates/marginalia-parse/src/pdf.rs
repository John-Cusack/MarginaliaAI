//! PDF text extraction, mirroring `modules/pdf_text.py` (pymupdf).
//!
//! The page-image split is honest about its one engine boundary: pymupdf's
//! text layer is MuPDF's, and no Rust crate reproduces it byte for byte.
//! [`extract_pages`] (via `pdf-extract`) reads the text layer; everything
//! after that — blank-page filtering, page joining, title fallback, the
//! metadata table — is the module's own logic and ports exactly, so any two
//! runs given the same page texts agree byte for byte.
//!
//! Deviations recorded, not hidden: page *content* comes from a different
//! engine (word order and spacing inside a page may differ from pymupdf),
//! and PDF metadata strings decode as UTF-16BE past a BOM else bytewise
//! (pymupdf applies PDFDocEncoding, which agrees on ASCII and most Latin).

use marginalia_types::sdk::ParsedDocument;
use marginalia_types::{Error, Result};
use serde_json::{Map, Value};

use crate::lower_suffix;
use crate::pystr::py_stem;

pub const MODULE_ID: &str = "pdf_text";
pub const MODULE_VERSION: &str = "1.0";
pub const DEFAULT_CHUNKER: &str = "prose_window";
pub const DEFAULT_DOCUMENT_TYPE: &str = "generic";

/// `detect`: the extension branch, then the magic bytes. The `mimetypes`
/// branch between them reads the OS table and stays caller-side.
pub fn detect(file_name: &str, head: &[u8]) -> (f64, String) {
    let suffix = lower_suffix(file_name);
    if suffix == ".pdf" {
        return (0.9, format!("extension '{suffix}' matches PDF"));
    }
    if head.len() >= 5 && head[..5] == *b"%PDF-" {
        return (0.9, "file starts with PDF magic bytes".to_owned());
    }
    (0.0, "not detected as PDF".to_owned())
}

/// PDF metadata strings: UTF-16BE past a BOM, bytewise otherwise.
pub fn pdf_string(bytes: &[u8]) -> String {
    if let Some(stripped) = bytes.strip_prefix(&[0xFE, 0xFF]) {
        let units: Vec<u16> = stripped
            .as_chunks::<2>()
            .0
            .iter()
            .map(|pair| u16::from_be_bytes(*pair))
            .collect();
        return String::from_utf16_lossy(&units);
    }
    bytes.iter().map(|b| *b as char).collect()
}

/// Document-info strings by key (`Title`, `Author`, `Subject`, `Creator`,
/// `CreationDate`), missing or non-string as `""`.
pub fn info_string(raw: &[u8], key: &str) -> Result<String> {
    let document = lopdf_document(raw)?;
    info_string_in(&document, key)
}

fn lopdf_document(raw: &[u8]) -> Result<lopdf::Document> {
    lopdf::Document::load_mem(raw).map_err(|err| Error::Parse(format!("not a PDF: {err}")))
}

fn info_string_in(document: &lopdf::Document, key: &str) -> Result<String> {
    let info_id = document.trailer.get(b"Info").ok();
    let info_id = match info_id {
        Some(lopdf::Object::Reference(id)) => Some(*id),
        _ => None,
    };
    let Some(info_id) = info_id else {
        return Ok(String::new());
    };
    let Ok(info) = document.get_dictionary(info_id) else {
        return Ok(String::new());
    };
    let Ok(object) = info.get(key.as_bytes()) else {
        return Ok(String::new());
    };
    let bytes = match object {
        lopdf::Object::String(bytes, _) => bytes.clone(),
        _ => return Ok(String::new()),
    };
    Ok(pdf_string(&bytes))
}

/// Page count from the catalog.
pub fn page_count(raw: &[u8]) -> Result<u32> {
    let document = lopdf_document(raw)?;
    Ok(document.get_pages().len() as u32)
}

/// The text layer, one entry per page, in page order.
///
/// `extract_text_from_mem_by_pages` re-parses these bytes with the same
/// `Document::load_mem` that `lopdf_document` above already accepted, so its
/// `Err` (that same parse failing) is unreachable on arrival: same pure
/// function, same bytes. What does escape is a panic — on malformed content
/// (unknown fonts, dangling references) it unwinds where fitz substitutes —
/// so the call runs behind an unwind guard and answers errors, never crashes.
/// (Re-verify against `pdf-extract`'s `extract_text_from_mem_by_pages` if the
/// dependency upgrades: the proof reads its current body.)
pub fn extract_pages(raw: &[u8]) -> Result<Vec<String>> {
    // Reject non-PDFs before the extraction below: the `expect` leans on
    // `load_mem` succeeding, and this function is public, so the check lives
    // here rather than only in `parse_bytes`.
    let _ = lopdf_document(raw)?;
    match std::panic::catch_unwind(|| pdf_extract::extract_text_from_mem_by_pages(raw)) {
        Ok(pages) => Ok(pages.expect("bytes lopdf accepted parse identically here")),
        Err(_) => Err(Error::Parse("PDF text extraction failed".to_owned())),
    }
}

/// Parse a PDF's bytes: text layer plus assembly.
pub fn parse_bytes(raw: &[u8], file_name: &str) -> Result<ParsedDocument> {
    let document = lopdf_document(raw)?;
    let pages = extract_pages(raw)?;
    let meta = PdfMeta {
        title: info_string_in(&document, "Title").unwrap_or_default(),
        author: info_string_in(&document, "Author").unwrap_or_default(),
        subject: info_string_in(&document, "Subject").unwrap_or_default(),
        creator: info_string_in(&document, "Creator").unwrap_or_default(),
        creation_date: info_string_in(&document, "CreationDate").unwrap_or_default(),
    };
    Ok(assemble(
        &pages,
        document.get_pages().len() as u64,
        &meta,
        file_name,
    ))
}

/// The metadata the assembly reads.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PdfMeta {
    pub title: String,
    pub author: String,
    pub subject: String,
    pub creator: String,
    pub creation_date: String,
}

/// The module's `_extract` body past the page loop: blank pages dropped,
/// pages joined, title from metadata or the first short line, and the
/// metadata table with empty strings filtered out (integers never are).
pub fn assemble(
    pages: &[String],
    page_count: u64,
    meta: &PdfMeta,
    file_name: &str,
) -> ParsedDocument {
    let kept: Vec<&str> = pages
        .iter()
        .map(String::as_str)
        .filter(|page| !marginalia_text::chars::strip(page).is_empty())
        .collect();
    let full_text = kept.join("\n\n");

    let mut title = meta.title.clone();
    if marginalia_text::chars::strip(&title).is_empty() {
        title = String::new();
        for line in full_text.split('\n') {
            let stripped = marginalia_text::chars::strip(line);
            if !stripped.is_empty() && stripped.chars().count() <= 300 {
                title = stripped.to_owned();
                break;
            }
        }
        if title.is_empty() {
            title = py_stem(file_name).to_owned();
        }
    }

    let mut metadata = Map::new();
    metadata.insert("page_count".to_owned(), Value::from(page_count));
    metadata.insert(
        "char_count".to_owned(),
        Value::from(full_text.chars().count() as u64),
    );
    metadata.insert("file_name".to_owned(), Value::from(file_name));
    for (key, value) in [
        ("pdf_author", meta.author.as_str()),
        ("pdf_subject", meta.subject.as_str()),
        ("pdf_creator", meta.creator.as_str()),
        ("pdf_creation_date", meta.creation_date.as_str()),
    ] {
        metadata.insert(key.to_owned(), Value::from(value));
    }
    // Empty strings drop; anything else (notably the integers) stays.
    metadata.retain(|_, value| value != "");

    ParsedDocument {
        title: Some(title),
        text: full_text,
        document_type: DEFAULT_DOCUMENT_TYPE.to_owned(),
        language: None,
        metadata,
        sections: Vec::new(),
        structural_locators: Vec::new(),
    }
}
