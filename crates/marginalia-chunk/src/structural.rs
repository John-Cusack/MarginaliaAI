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
///
/// `heading`, `level`, and `page` are whatever JSON the parser put there:
/// upstream only tests them for truthiness and copies them into the locator,
/// so a numeric heading or a string level travels unchanged.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct SectionInput {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub heading: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub level: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub page: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub char_start: Option<Offset>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub char_end: Option<Offset>,
}

/// A section offset as Python holds it. `bool` is an `int` there (`True`
/// slices as 1), so JSON booleans arrive as integers. A float cannot slice a
/// string (`TypeError`), but where upstream never slices, an integral float
/// passes through as the number it equals.
#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
#[serde(untagged)]
pub enum Offset {
    Int(i64),
    /// Kept apart from `Int` only so error messages print `True`, as
    /// Python's f-string does; it slices and offsets as 0/1.
    Bool(bool),
    Float(f64),
}

impl Offset {
    /// The integer Python slices and stores with, or the `TypeError` a
    /// float raises the moment it is used as a slice index.
    fn index(self) -> Result<i64> {
        match self {
            Offset::Int(i) => Ok(i),
            Offset::Bool(b) => Ok(i64::from(b)),
            Offset::Float(_) => Err(Error::Type(SLICE_TYPE_ERROR.to_owned())),
        }
    }

    /// The draft offset `PassageDraft` validates this into: integral floats
    /// are accepted as the integer they equal, fractional ones refused.
    fn draft_value(self) -> Result<i64> {
        match self {
            Offset::Float(f) if f.fract() != 0.0 || !f.is_finite() => Err(Error::Validation(
                format!("char_start should be a valid integer, got a number with a fractional part: {f}"),
            )),
            Offset::Float(f) => Ok(f as i64),
            other => other.index(),
        }
    }
}

impl std::fmt::Display for Offset {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Offset::Int(i) => write!(f, "{i}"),
            Offset::Bool(true) => f.write_str("True"),
            Offset::Bool(false) => f.write_str("False"),
            Offset::Float(x) => write!(f, "{x:?}"),
        }
    }
}

impl From<i64> for Offset {
    fn from(value: i64) -> Self {
        Offset::Int(value)
    }
}

impl<'de> Deserialize<'de> for Offset {
    fn deserialize<D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> std::result::Result<Self, D::Error> {
        use serde::de::Error as _;
        match Value::deserialize(deserializer)? {
            Value::Bool(b) => Ok(Offset::Bool(b)),
            Value::Number(n) => match (n.as_i64(), n.as_f64()) {
                (Some(i), _) => Ok(Offset::Int(i)),
                // A JSON integer too large for i64 still reads as f64; only a
                // written fraction or exponent is a Python float.
                (None, Some(f)) if n.to_string().contains(['.', 'e', 'E']) => Ok(Offset::Float(f)),
                _ => Err(D::Error::custom(format!(
                    "offset {n} is outside the i64 range"
                ))),
            },
            other => Err(D::Error::custom(format!(
                "offsets are integers, got {other}"
            ))),
        }
    }
}

const SLICE_TYPE_ERROR: &str = "slice indices must be integers or None or have an __index__ method";

/// Python `text[start:end]` bounds (step 1) over `len` characters: negative
/// indices count from the end, everything clamps, and an end before the start
/// is an empty slice.
fn py_slice_bounds(len: usize, start: i64, end: i64) -> (usize, usize) {
    let len = len as i64;
    let norm = |i: i64| if i < 0 { (i + len).max(0) } else { i.min(len) };
    let (start, end) = (norm(start), norm(end));
    (start as usize, end.max(start) as usize)
}

/// Python truthiness of a JSON value (`if value := section.get(key)`).
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
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
        let mut cursor: i64 = 0;
        let mut position: i64 = 0;

        for section in sections {
            let raw = section_text(section, full.as_ref())?;
            if raw.chars().all(marginalia_text::chars::is_space) {
                continue;
            }
            let (start, end, text) = locate(section, raw, full.as_ref(), cursor)?;
            // A float end only ever lands here without a document, where the
            // cursor is never searched from; its integer part stands in.
            cursor = match end {
                Offset::Float(f) => f as i64,
                other => other.index()?,
            };

            // `if key := section.get(key)`: falsy values ("" / 0 / null)
            // are omitted from the locator, exactly as upstream; truthy ones
            // travel as whatever the parser put there.
            let mut locator = Map::new();
            let mut section_meta = metadata.cloned().unwrap_or_default();
            if let Some(heading) = section.heading.as_ref().filter(|h| truthy(h)) {
                locator.insert("heading".to_owned(), heading.clone());
                section_meta.insert("section_heading".to_owned(), heading.clone());
            }
            if let Some(level) = section.level.as_ref().filter(|l| truthy(l)) {
                locator.insert("level".to_owned(), level.clone());
            }
            if let Some(page) = section.page.as_ref().filter(|p| truthy(p)) {
                locator.insert("page".to_owned(), page.clone());
            }

            let start = start.draft_value()?;
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
        start: i64,
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
                char_start: start,
                char_end: start + len as i64,
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
                window.char_start += start;
                window.char_end += start;
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
/// The section's prose, read back from `full_text` when not carried.
///
/// Python slices `full_text[start:end]`: negative offsets count from the
/// end and everything clamps, so a span running past the document yields
/// the text that is there rather than nothing.
fn section_text<'a>(section: &'a SectionInput, full: Option<&'a CharText<'a>>) -> Result<&'a str> {
    if let Some(text) = section.text.as_deref() {
        return Ok(text);
    }
    match (full, section.char_start, section.char_end) {
        (Some(doc), Some(start), Some(end)) => {
            let (s, e) = py_slice_bounds(doc.len(), start.index()?, end.index()?);
            Ok(doc.slice(s, e))
        }
        _ => Ok(""),
    }
}

/// Resolve a section to its offsets and prose.
///
/// Offsets the section reports are returned as reported (a draft checks
/// them), after a comparison against the document when there is one.
/// Without offsets the prose is found in the document and trimmed.
fn locate(
    section: &SectionInput,
    raw: &str,
    full: Option<&CharText>,
    cursor: i64,
) -> Result<(Offset, Offset, String)> {
    if let (Some(start), Some(end)) = (section.char_start, section.char_end) {
        if let Some(doc) = full {
            let (s, e) = py_slice_bounds(doc.len(), start.index()?, end.index()?);
            if doc.slice(s, e) != raw {
                return Err(Error::Chunking(format!(
                    "Section reports span ({start}, {end}) but the text there \
                     does not match the section text."
                )));
            }
        }
        return Ok((start, end, raw.to_owned()));
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
    let found = py_find(doc, raw, cursor).or_else(|| py_find(doc, raw, 0));
    let Some(at) = found else {
        let head: String = raw.chars().take(80).collect();
        // Python `{raw[:80]!r}`: `{:?}` quotes double where CPython prefers
        // single (proven by the seam differential on plain section text).
        let head = marginalia_text::repr::py_repr_str(&head);
        return Err(Error::Chunking(format!(
            "Section text not found in the document: {head}"
        )));
    };
    let (s, e) = marginalia_text::spans::trim_span(doc.chars(), at, at + raw.chars().count());
    Ok((
        Offset::Int(s as i64),
        Offset::Int(e as i64),
        doc.slice(s, e).to_owned(),
    ))
}

/// Char-offset `str.find(needle, from)`. `from` is read as a slice start:
/// negative counts from the end (clamped at 0), and past the end finds
/// nothing — even an empty needle.
fn py_find(doc: &CharText, needle: &str, from: i64) -> Option<usize> {
    let len = doc.len() as i64;
    let from = if from < 0 { (from + len).max(0) } else { from };
    if from > len {
        return None;
    }
    let from = from as usize;
    let byte = doc.byte_of(from);
    let at = doc.slice(from, doc.len()).find(needle)?;
    Some(doc.char_of(byte + at))
}
