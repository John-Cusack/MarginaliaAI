//! Phase 3 document parsers: the pure cores of the ingestion modules.
//!
//! Python sources: `modules/{plain_text,markdown,html,epub,tei_xml,
//! pdf_text}.py`. Each parser returns an SDK [`ParsedDocument`], built here
//! from bytes plus the file name — file IO and the `mimetypes` branch of
//! `detect` stay caller-side (`mimetypes` reads the OS table, so it is not
//! deterministic across machines and never ports).
//!
//! The one-off corpus scripts (`scripts/{bible_layout,wlc_extract,
//! ingest_wlc,load_versification,discover_overlaps}.py`) port as crate
//! binaries (`src/bin/`), not library code: they ran once over a corpus,
//! not per document.
//!
//! All offsets are character offsets, exactly as Python string indices are.
//! Byte readers follow each module's own rules: strict UTF-8 where the
//! module reads strict (`plain_text`, `markdown`, TEI without a declared
//! encoding), U+FFFD replacement where it passes `errors="replace"`
//! (`html`, EPUB chapters).

pub mod case_tables;
pub mod epub;
pub mod html;
pub mod html_entities;
pub mod markdown;
pub mod normalize_entities;
pub mod pdf;
pub mod plain_text;
pub mod pystr;
pub mod tei;
pub mod xml;

use marginalia_types::{Error, Result};

/// Decode UTF-8 the way `errors="replace"` does: one U+FFFD per bad byte.
pub fn decode_replace(raw: &[u8]) -> String {
    let (text, _, _) = encoding_rs::UTF_8.decode(raw);
    text.into_owned()
}

/// Decode strict UTF-8 the way `Path.read_text(encoding="utf-8")` does:
/// any bad byte is a parse failure, not a substitution.
pub fn decode_strict(raw: &[u8]) -> Result<&str> {
    std::str::from_utf8(raw).map_err(|err| Error::Parse(format!("not valid UTF-8: {err}")))
}

/// Text-mode newline translation: `Path.read_text` and `open()` in text
/// mode turn CRLF and CR into LF on the way in. Byte readers reproduce the
/// read, so they translate the same way before parsing.
pub fn translate_newlines(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

/// `pathlib` lowercased suffix of the final path component (`""` when none).
/// The detect messages quote this lowered form, so it is computed once here.
pub fn lower_suffix(path: &str) -> String {
    let base = path.rsplit(['/', '\\']).next().unwrap_or(path);
    let suffix = match base.rfind('.') {
        Some(0) | None => "",
        Some(i) => &base[i..],
    };
    suffix.to_lowercase()
}

/// One section boundary addressed into the canonical text.
///
/// `heading`, `level`, and `href` are optional because EPUB omits empty ones
/// and TEI drops missing headings while keeping empty ones; [`section_map`]
/// emits exactly the keys each parser's contract keeps.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Section {
    pub char_start: usize,
    pub char_end: usize,
    pub heading: Option<String>,
    pub level: Option<u64>,
    pub href: Option<String>,
}

/// The section table as plain maps, one per section, with only the keys the
/// parser kept.
pub fn section_map(section: &Section) -> serde_json::Map<String, serde_json::Value> {
    let mut map = serde_json::Map::new();
    map.insert(
        "char_start".to_owned(),
        serde_json::Value::from(section.char_start as u64),
    );
    map.insert(
        "char_end".to_owned(),
        serde_json::Value::from(section.char_end as u64),
    );
    if let Some(heading) = &section.heading {
        map.insert(
            "heading".to_owned(),
            serde_json::Value::from(heading.clone()),
        );
    }
    if let Some(level) = section.level {
        map.insert("level".to_owned(), serde_json::Value::from(level));
    }
    if let Some(href) = &section.href {
        map.insert("href".to_owned(), serde_json::Value::from(href.clone()));
    }
    map
}
