//! Phase 2 pure retrieval math: chunkers, read windows, and fusion.
//!
//! Python sources: `services/ingestion/chunking/{fixed_window,prose_window,
//! structural,whole_or_paragraph}.py`, `services/search/{windows,fusion,
//! langconfig}.py`. All offsets are character offsets, exactly as Python
//! string indices are.
//!
//! The chunkers are `async` in Python but perform no IO, so they are plain
//! synchronous functions here. Everything that touches a repository stays
//! Python: `HybridSearchService::find_passages`, `PassageWindowReader::read`
//! (ancestor/slice reads), `HitSourceReader::read` (document/version reads),
//! `OffsetRecoveryService` (gap scan + updates), and the `filter_extensions`
//! clause builders (SQLAlchemy selects — a Phase 5 `sqlx` concern).
//!
//! Two deliberate non-ports:
//! - `offsets.py` contributes no new pure logic: span-less recovery is
//!   `CanonicalIndex::find` plus a `collapse_whitespace` equality check,
//!   both already in `marginalia_text::anchoring`.
//! - `hit_source.py`'s pure content is one conjunction (quotable only with
//!   canonical text *and* offsets); a wrapper would add surface, not safety.
//!
//! [`CharText`] is the internal bridge both chunkers stand on. Python indexes
//! `str` by Unicode scalar value; Rust indexes `&str` by byte. The text is
//! decoded once into chars plus a byte-offset table, so every span stays in
//! Python's address space and slicing back to `&str` is exact.

pub mod fixed_window;
pub mod fusion;
pub mod langconfig;
pub mod prose_window;
pub mod structural;
pub mod whole_or_paragraph;
pub mod windows;

/// A `&str` decoded once, addressable in Python (char) offsets.
#[derive(Debug, Clone)]
pub(crate) struct CharText<'a> {
    text: &'a str,
    /// Byte index where each char starts; length `chars + 1`, last is `len`.
    bytes: Vec<usize>,
    chars: Vec<char>,
}

impl<'a> CharText<'a> {
    pub(crate) fn new(text: &'a str) -> Self {
        let mut bytes: Vec<usize> = text.char_indices().map(|(b, _)| b).collect();
        bytes.push(text.len());
        let chars: Vec<char> = text.chars().collect();
        debug_assert_eq!(bytes.len(), chars.len() + 1);
        Self { text, bytes, chars }
    }

    /// Number of chars — what Python calls `len(text)`.
    pub(crate) fn len(&self) -> usize {
        self.chars.len()
    }

    pub(crate) fn chars(&self) -> &[char] {
        &self.chars
    }

    /// `text[start:end]` in char offsets, as `&str`.
    pub(crate) fn slice(&self, start: usize, end: usize) -> &'a str {
        &self.text[self.bytes[start]..self.bytes[end]]
    }

    /// Byte index of a char offset (clamped), for `str::find`-style searches.
    pub(crate) fn byte_of(&self, char_idx: usize) -> usize {
        self.bytes[char_idx.min(self.len())]
    }

    /// Char offset of a byte index known to sit on a char boundary.
    pub(crate) fn char_of(&self, byte_idx: usize) -> usize {
        self.bytes.partition_point(|&b| b < byte_idx)
    }
}

/// Identity a chunker stamps on its drafts. One shared constructor instead
/// of four copies keeps the stamp from drifting per chunker.
pub(crate) trait ChunkerMeta {
    const ID: &'static str;
    const VERSION: &'static str;
}

/// A draft sliced from `ct`, stamped for `C`. Callers pass pre-trimmed spans;
/// the text/slice agreement is what the contract suite checks.
pub(crate) fn make_draft<C: ChunkerMeta>(
    ct: &CharText,
    start: usize,
    end: usize,
    position: i64,
    metadata: Option<&serde_json::Map<String, serde_json::Value>>,
    rate: f64,
) -> marginalia_types::sdk::PassageDraft {
    let chunk_text = ct.slice(start, end);
    marginalia_types::sdk::PassageDraft {
        position,
        char_start: start as i64,
        char_end: end as i64,
        locator: serde_json::Map::new(),
        text: chunk_text.to_owned(),
        token_count: Some(marginalia_text::tokens::approx_tokens(
            chunk_text,
            Some(rate),
        )),
        chunker: C::ID.to_owned(),
        chunker_version: C::VERSION.to_owned(),
        metadata: metadata.cloned().unwrap_or_default(),
        node_id: None,
    }
}
