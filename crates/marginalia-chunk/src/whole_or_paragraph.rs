//! Whole-or-paragraph chunking, mirroring
//! `services/ingestion/chunking/whole_or_paragraph.py`.
//!
//! Short docs stay whole, long docs split by paragraph — but never past the
//! point where an embedding model truncates the result. A book index that is
//! one "paragraph" of thousands of tokens is broken at a line or word seam.

use marginalia_text::chars::is_space;
use marginalia_text::spans::{split_span, trim_span};
use marginalia_text::tokens::{
    approx_tokens, chars_per_token, min_chars_per_token, token_budget_chars, ABSOLUTE_MAX_TOKENS,
};
use marginalia_types::sdk::PassageDraft;
use marginalia_types::Result;
use serde_json::{Map, Value};

use crate::{make_draft, CharText, ChunkerMeta};

pub const ID: &str = "whole_or_paragraph";
pub const CONSUMES: &str = "text";
/// 3.0 breaks a past-the-ceiling paragraph at a seam; 4.0 measures the
/// ceiling in real tokens, moving only non-Latin documents.
pub const VERSION: &str = "4.0";

/// A paragraph is this chunker's unit, and it holds to that — but not past
/// the embedder's reach. Sized to match the shared absolute-max contract.
pub const CEILING_TOKENS: i64 = ABSOLUTE_MAX_TOKENS;
pub const DEFAULT_THRESHOLD_TOKENS: i64 = 600;

/// Paragraph spans as `(start, end)` char offsets, blank ones dropped.
///
/// Mirrors `_PARA_SPLIT = re.compile(r"\n\s*\n")`. Greed-with-backtrack
/// reduces to "the last `\n` in the whitespace run": from each `\n`, the
/// longest all-space stretch ending in `\n` is the match, and scanning
/// resumes past it exactly as `finditer` does.
pub fn paragraph_spans(text: &str) -> Vec<(usize, usize)> {
    let ct = CharText::new(text);
    paragraph_spans_of(ct.chars())
}

/// [`paragraph_spans`] over an already-decoded slice, so `chunk` pays the
/// decode once instead of twice.
pub(crate) fn paragraph_spans_of(chars: &[char]) -> Vec<(usize, usize)> {
    let mut spans = Vec::new();
    let mut start = 0;
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '\n' {
            let mut j = i + 1;
            let mut last_nl: Option<usize> = None;
            while j < chars.len() && is_space(chars[j]) {
                if chars[j] == '\n' {
                    last_nl = Some(j);
                }
                j += 1;
            }
            if let Some(p) = last_nl {
                spans.push((start, i));
                start = p + 1;
                i = p + 1;
                continue;
            }
        }
        i += 1;
    }
    spans.push((start, chars.len()));

    spans
        .into_iter()
        .map(|(s, e)| trim_span(chars, s, e))
        .filter(|&(s, e)| e > s)
        .collect()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WholeOrParagraphChunker {
    pub threshold_tokens: i64,
}

impl Default for WholeOrParagraphChunker {
    fn default() -> Self {
        Self::new(DEFAULT_THRESHOLD_TOKENS)
    }
}
impl ChunkerMeta for WholeOrParagraphChunker {
    const ID: &'static str = ID;
    const VERSION: &'static str = VERSION;
}

impl WholeOrParagraphChunker {
    pub fn new(threshold_tokens: i64) -> Self {
        Self { threshold_tokens }
    }

    /// Unbounded, deliberately: one long paragraph yields one long passage.
    /// Declared `None` so the contract suite treats it as a decision — but
    /// the absolute ceiling still applies, because a passage the embedder
    /// truncates is unreachable whatever principle produced it.
    pub fn max_passage_tokens(&self) -> Option<i64> {
        None
    }

    pub fn chunk(
        &self,
        text: &str,
        metadata: Option<&Map<String, Value>>,
    ) -> Result<Vec<PassageDraft>> {
        if text.chars().all(is_space) {
            return Ok(Vec::new());
        }
        let ct = CharText::new(text);
        let rate = chars_per_token(text);
        // The ceiling is budgeted against the densest script in the document,
        // not its average: this limit admits no exemption, so it has to hold
        // for a passage denser than the book around it.
        let ceiling_chars =
            token_budget_chars(CEILING_TOKENS, min_chars_per_token(text)).max(1) as usize;

        // Short docs stay whole — even multi-paragraph ones. Without this, a
        // three-paragraph note becomes three chunks against a threshold that
        // says it fits in one.
        if approx_tokens(text, Some(rate)) <= self.threshold_tokens && ct.len() <= ceiling_chars {
            return Ok(vec![make_draft::<Self>(
                &ct,
                0,
                ct.len(),
                0,
                metadata,
                rate,
            )]);
        }

        let mut spans = Vec::new();
        for (s, e) in paragraph_spans_of(ct.chars()) {
            // Infallible: paragraph spans are ordered and in-bounds by
            // construction (blank ones dropped), and the ceiling floors at 1
            // — the only two rejections the checked entry has.
            spans.extend(split_span(ct.chars(), s, e, ceiling_chars));
        }
        Ok(spans
            .into_iter()
            .enumerate()
            .map(|(position, (s, e))| {
                make_draft::<Self>(&ct, s, e, position as i64, metadata, rate)
            })
            .collect())
    }
}
