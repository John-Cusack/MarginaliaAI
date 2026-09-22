//! Structural chunking, mirroring
//! `services/ingestion/chunking/structural.py`.
//!
//! Sections stay the addressing unit; an oversized section is windowed with
//! the prose chunker and each piece keeps the heading in its locator, shifted
//! back into document offsets.

use marginalia_text::tokens::{approx_tokens, chars_per_token};
use marginalia_types::sdk::PassageDraft;
use marginalia_types::{Error, Result};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::prose_window::ProseWindowChunker;
use crate::CharText;

pub const ID: &str = "structural";
pub const CONSUMES: &str = "sections";
/// 3.0 windows oversized sections; 4.0 estimates script-aware rates, moving
/// only non-Latin boundaries.
pub const VERSION: &str = "4.0";

/// One parser section. Python reads a dict; the keys it looks at are
/// `text`, `heading`, `level`, `page`, `char_start`, `char_end`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct SectionInput {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub heading: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub level: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub page: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub char_start: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub char_end: Option<i64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StructuralChunker {
    pub max_tokens: i64,
    pub overlap_tokens: i64,
}

impl Default for StructuralChunker {
    fn default() -> Self {
        Self::new(500, 50)
    }
}

impl StructuralChunker {
    pub fn new(max_tokens: i64, overlap_tokens: i64) -> Self {
        Self {
            max_tokens,
            overlap_tokens,
        }
    }

    pub fn max_passage_tokens(&self) -> Option<i64> {
        Some(self.max_tokens)
    }

    /// Chunk a section table. A section's prose comes from the section
    /// itself, or is read back from `full_text` when the parser hands over
    /// pure boundaries.
    pub fn chunk(
        &self,
        sections: &[SectionInput],
        metadata: Option<&Map<String, Value>>,
        full_text: Option<&str>,
    ) -> Result<Vec<PassageDraft>> {
        let full = full_text.map(CharText::new);
        let mut chunks = Vec::new();
        let mut cursor: usize = 0;
        let mut position: i64 = 0;

        for section in sections {
            let raw = section_text(section, full.as_ref());
            if raw.chars().all(marginalia_text::chars::is_space) {
                continue;
            }
            let (start, end, text) = locate(section, raw, full.as_ref(), cursor)?;
            cursor = end;

            // `if key := section.get(key)`: falsy values ("" / 0 / null)
            // are omitted from the locator, exactly as upstream.
            let mut locator = Map::new();
            if let Some(heading) = section.heading.as_deref().filter(|h| !h.is_empty()) {
                locator.insert("heading".to_owned(), Value::String(heading.to_owned()));
            }
            if let Some(level) = section.level.filter(|l| *l != 0) {
                locator.insert("level".to_owned(), Value::from(level));
            }
            if let Some(page) = section.page.as_ref().filter(|p| has_page(p)) {
                locator.insert("page".to_owned(), page.clone());
            }

            let mut section_meta = metadata.cloned().unwrap_or_default();
            if let Some(heading) = section.heading.as_deref().filter(|h| !h.is_empty()) {
                section_meta.insert(
                    "section_heading".to_owned(),
                    Value::String(heading.to_owned()),
                );
            }

            for mut draft in self.drafts_for_section(&text, start, &locator, &section_meta)? {
                draft.position = position;
                position += 1;
                chunks.push(draft);
            }
        }
        Ok(chunks)
    }

    /// One passage for a section that fits; prose windows for one that does
    /// not. Window offsets come back relative to the section, so they are
    /// shifted by the section's own start.
    fn drafts_for_section(
        &self,
        text: &str,
        start: usize,
        locator: &Map<String, Value>,
        section_meta: &Map<String, Value>,
    ) -> Result<Vec<PassageDraft>> {
        // Measured on the section, not the document: a Greek passage quoted
        // inside an English book reads as English at the document level, and
        // a section estimated at 472 tokens that way really came to 947.
        let rate = chars_per_token(text);
        // Counted once: the draft below used to recount the same section.
        let tokens = approx_tokens(text, Some(rate));
        if tokens <= self.max_tokens {
            let len = text.chars().count();
            return Ok(vec![PassageDraft {
                position: 0,
                char_start: start as i64,
                char_end: (start + len) as i64,
                locator: locator.clone(),
                text: text.to_owned(),
                token_count: Some(tokens),
                chunker: ID.to_owned(),
                chunker_version: VERSION.to_owned(),
                metadata: section_meta.clone(),
                node_id: None,
            }]);
        }

        let windows = ProseWindowChunker::new(self.max_tokens, self.overlap_tokens)
            .chunk(text, Some(section_meta))?;
        let total = windows.len();
        Ok(windows
            .into_iter()
            .enumerate()
            .map(|(index, mut window)| {
                window.char_start += start as i64;
                window.char_end += start as i64;
                window.chunker = ID.to_owned();
                window.chunker_version = VERSION.to_owned();
                // The heading travels with every piece: a fragment that has
                // lost its section is exactly the disconnected chunk this
                // chunker exists to avoid.
                let mut merged = locator.clone();
                merged.insert("section_part".to_owned(), Value::from((index + 1) as i64));
                merged.insert("section_parts".to_owned(), Value::from(total as i64));
                window.locator = merged;
                window
            })
            .collect())
    }
}
/// Python `if page := section.get("page")`: only truthy values travel.
fn has_page(page: &Value) -> bool {
    match page {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => {
            n.as_i64() != Some(0) && n.as_u64() != Some(0) && n.as_f64() != Some(0.0)
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// The section's prose, read back from `full_text` when not carried.
fn section_text<'a>(section: &'a SectionInput, full: Option<&'a CharText<'a>>) -> &'a str {
    if let Some(text) = section.text.as_deref() {
        return text;
    }
    match (full, section.char_start, section.char_end) {
        (Some(doc), Some(start), Some(end))
            if start >= 0 && end >= start && (end as usize) <= doc.len() =>
        {
            doc.slice(start as usize, end as usize)
        }
        _ => "",
    }
}

/// Resolve a section to document offsets and its trimmed prose.
fn locate(
    section: &SectionInput,
    raw: &str,
    full: Option<&CharText>,
    cursor: usize,
) -> Result<(usize, usize, String)> {
    if let (Some(start), Some(end)) = (section.char_start, section.char_end) {
        match full {
            Some(doc) => {
                let len = doc.len();
                if start < 0 || end < start || (end as usize) > len {
                    return Err(Error::Chunking(format!(
                        "Section reports span ({start}, {end}) outside a document of {len} chars."
                    )));
                }
                if doc.slice(start as usize, end as usize) != raw {
                    return Err(Error::Chunking(format!(
                        "Section reports span ({start}, {end}) but the text there \
                         does not match the section text."
                    )));
                }
            }
            None => {
                if start < 0 || end < start {
                    return Err(Error::Chunking(format!(
                        "Section reports span ({start}, {end}) with no document to check it against."
                    )));
                }
            }
        }
        return Ok((start as usize, end as usize, raw.to_owned()));
    }

    let Some(doc) = full else {
        return Err(Error::Chunking(
            "Structural chunking needs offsets: supply full_text, or give \
             each section char_start and char_end. Passages without a true \
             span cannot be cited or re-anchored."
                .to_owned(),
        ));
    };
    // Sections run in document order, so each search resumes where the last
    // match ended; repeated headings resolve to successive occurrences.
    // (The offsets module's cursor carries the same idea for passages.)
    let found = index_of(doc, raw, cursor).or_else(|| index_of(doc, raw, 0));
    let Some(at) = found else {
        let head: String = raw.chars().take(80).collect();
        return Err(Error::Chunking(format!(
            "Section text not found in the document: {head:?}"
        )));
    };
    let (s, e) = marginalia_text::spans::trim_span(doc.chars(), at, at + raw.chars().count());
    Ok((s, e, doc.slice(s, e).to_owned()))
}

/// Char-offset `str.find(needle, from)`: forward scan, then the caller falls
/// back to 0. `cursor` past the end finds nothing, exactly as in Python.
fn index_of(doc: &CharText, needle: &str, from: usize) -> Option<usize> {
    // No empty-needle guard: blank sections never reach `locate`, and
    // `str::find("")` returns 0, which maps back to `from` anyway.
    let from = from.min(doc.len());
    let byte = doc.byte_of(from);
    let at = doc.slice(from, doc.len()).find(needle)?;
    Some(doc.char_of(byte + at))
}
