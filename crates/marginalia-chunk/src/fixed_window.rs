//! Simple character/token windows, mirroring
//! `services/ingestion/chunking/fixed_window.py`.
//!
//! The configured window is characters of *English*: it is re-derived per
//! document through [`chars_per_token`](marginalia_text::tokens::chars_per_token),
//! so the token budget is the constant and ASCII documents are unaffected.

use marginalia_text::spans::trim_span;
use marginalia_text::tokens::{chars_per_token, DEFAULT_CHARS_PER_TOKEN};
use marginalia_types::sdk::PassageDraft;
use serde_json::{Map, Value};

use crate::{make_draft, CharText, ChunkerMeta};

pub const ID: &str = "fixed_window";
pub const CONSUMES: &str = "text";
/// 2.0 trims the span instead of stripping the text; 3.0 budgets tokens, so
/// CJK windows hold ~750 characters rather than 2,000 for the same 500 tokens.
pub const VERSION: &str = "3.0";

pub const DEFAULT_WINDOW_CHARS: i64 = 2000;
pub const DEFAULT_OVERLAP_CHARS: i64 = 200;

/// What `chunk` takes: characters of English, re-scaled per script.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FixedWindowChunker {
    pub window_chars: i64,
    pub overlap_chars: i64,
}

impl Default for FixedWindowChunker {
    fn default() -> Self {
        Self::new(DEFAULT_WINDOW_CHARS, DEFAULT_OVERLAP_CHARS)
    }
}

impl ChunkerMeta for FixedWindowChunker {
    const ID: &'static str = ID;
    const VERSION: &'static str = VERSION;
}

impl FixedWindowChunker {
    pub fn new(window_chars: i64, overlap_chars: i64) -> Self {
        Self {
            window_chars,
            overlap_chars,
        }
    }

    /// The window as a token budget, honoured in every script.
    pub fn max_passage_tokens(&self) -> i64 {
        ((self.window_chars as f64 / DEFAULT_CHARS_PER_TOKEN) as i64).max(1)
    }

    pub fn chunk(&self, text: &str, metadata: Option<&Map<String, Value>>) -> Vec<PassageDraft> {
        // `not text.strip()`: no char outside the verified `isspace` set.
        // `char::is_whitespace` alone misses U+001C-U+001F.
        if text.chars().all(marginalia_text::chars::is_space) {
            return Vec::new();
        }
        let ct = CharText::new(text);
        let rate = chars_per_token(text);
        let scale = rate / DEFAULT_CHARS_PER_TOKEN;
        // Python `int()` truncates toward zero; `as` does the same. Floors
        // are pinned before the cast, so the walk below stays in `usize`.
        let window = ((self.window_chars as f64 * scale) as i64).max(1) as usize;
        let overlap = ((self.overlap_chars as f64 * scale) as i64).max(0) as usize;

        let mut chunks = Vec::new();
        let mut start = 0;
        let mut position: i64 = 0;
        while start < ct.len() {
            let end = (start + window).min(ct.len());
            let (s, e) = trim_span(ct.chars(), start, end);
            if e > s {
                chunks.push(make_draft::<Self>(&ct, s, e, position, metadata, rate));
                position += 1;
            }
            if end >= ct.len() {
                break;
            }
            // `start + 1` so an overlap >= window cannot stall the walk.
            start = end.saturating_sub(overlap).max(start + 1);
        }
        chunks
    }
}
