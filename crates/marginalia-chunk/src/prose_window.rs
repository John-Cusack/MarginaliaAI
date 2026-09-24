//! Sentence-boundary-aware sliding windows, mirroring
//! `services/ingestion/chunking/prose_window.py`.
//!
//! Chunks slice the original text from the first sentence's start to the
//! last's end, so whitespace survives and offsets stay true — the old
//! `" ".join(sentences)` rebuild collapsed every run and made spans
//! unrecoverable.

use marginalia_text::chars::is_space;
use marginalia_text::spans::{cap_spans, split_span};
use marginalia_text::tokens::{approx_tokens, chars_per_token, token_budget_chars};
use marginalia_types::sdk::PassageDraft;
use marginalia_types::Result;
use serde_json::{Map, Value};

use crate::{make_draft, CharText, ChunkerMeta};

pub const ID: &str = "prose_window";
pub const CONSUMES: &str = "text";
/// 3.0 breaks an over-window unit at a word/line seam instead of emitting it
/// whole; 4.0 measures script-aware rates, moving only non-Latin boundaries.
pub const VERSION: &str = "4.0";

/// Sentence spans as `(start, end)` char offsets into `text`.
///
/// Mirrors `_SENT_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")`: after a
/// `.`/`!`/`?`, a greedy whitespace run, then an ASCII capital. The scanner is
/// equivalent by construction — the lookbehind fixes the run's start, greed
/// with backtrack reduces to "longest run, then one capital", and `finditer`
/// resumes at the run's end over characters that cannot re-trigger.
pub fn sentence_spans(text: &str) -> Vec<(usize, usize)> {
    let ct = CharText::new(text);
    sentence_spans_of(ct.chars())
}

/// [`sentence_spans`] over an already-decoded slice, so `chunk` pays the
/// decode once instead of twice.
pub(crate) fn sentence_spans_of(chars: &[char]) -> Vec<(usize, usize)> {
    let mut spans = Vec::new();
    let mut start = 0;
    let mut i = 0;
    while i < chars.len() {
        if matches!(chars[i], '.' | '!' | '?') && i + 1 < chars.len() && is_space(chars[i + 1]) {
            let mut j = i + 1;
            while j < chars.len() && is_space(chars[j]) {
                j += 1;
            }
            if j < chars.len() && chars[j].is_ascii_uppercase() {
                spans.push((start, i + 1));
                start = j;
                i = j;
                continue;
            }
        }
        i += 1;
    }
    if start < chars.len() {
        spans.push((start, chars.len()));
    }
    spans
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProseWindowChunker {
    pub max_tokens: i64,
    pub overlap_tokens: i64,
}

impl Default for ProseWindowChunker {
    fn default() -> Self {
        Self::new(500, 50)
    }
}

impl ChunkerMeta for ProseWindowChunker {
    const ID: &'static str = ID;
    const VERSION: &'static str = VERSION;
}

impl ProseWindowChunker {
    pub fn new(max_tokens: i64, overlap_tokens: i64) -> Self {
        Self {
            max_tokens,
            overlap_tokens,
        }
    }

    pub fn max_passage_tokens(&self) -> Option<i64> {
        Some(self.max_tokens)
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
        // Measured once for the document: the script mix of a sentence is the
        // script mix of the book it came from; measuring per span would put an
        // O(n) scan inside the loop below.
        let rate = chars_per_token(text);
        let pieces: Vec<(usize, usize)> = sentence_spans_of(ct.chars())
            .into_iter()
            .flat_map(|span| fit_to_window(&ct, span, self.max_tokens, rate))
            .collect();
        // `cap_spans` is the one fallible pass: it rejects `max_tokens < 1`.
        let spans = cap_spans(ct.chars(), &pieces, self.max_tokens)?;

        let mut chunks = Vec::new();
        // Each span carries its count, measured once at push: the flush
        // below used to re-slice and recount every kept span, plus a fresh
        // `Vec` per flush.
        let mut window: Vec<((usize, usize), i64)> = Vec::new();
        let mut window_tokens: i64 = 0;
        let mut position: i64 = 0;
        for span in spans {
            let span_tok = tokens_of_span(&ct, span, rate);
            if !window.is_empty() && window_tokens + span_tok > self.max_tokens {
                chunks.push(make_draft::<Self>(
                    &ct,
                    window[0].0 .0,
                    window[window.len() - 1].0 .1,
                    position,
                    metadata,
                    rate,
                ));
                position += 1;
                // The trailing spans that fit the overlap budget, with their
                // memoized counts — the same scan `overlap_window` ran, minus
                // the recount.
                let mut keep_from = window.len();
                let mut kept_tokens: i64 = 0;
                for (i, &(_, t)) in window.iter().enumerate().rev() {
                    if kept_tokens + t > self.overlap_tokens {
                        break;
                    }
                    kept_tokens += t;
                    keep_from = i;
                }
                window.drain(..keep_from);
                window_tokens = kept_tokens;
            }
            window.push((span, span_tok));
            window_tokens += span_tok;
        }
        let (s, e) = (window[0].0 .0, window[window.len() - 1].0 .1);
        chunks.push(make_draft::<Self>(&ct, s, e, position, metadata, rate));
        Ok(chunks)
    }
}

/// Break a unit that will not fit, at the best seam available.
///
/// Infallible: spans come from the text being split and the budget floors at
/// 1, which is exactly [`split_span`]'s contract.
fn fit_to_window(
    ct: &CharText,
    span: (usize, usize),
    max_tokens: i64,
    rate: f64,
) -> Vec<(usize, usize)> {
    split_span(
        ct.chars(),
        span.0,
        span.1,
        token_budget_chars(max_tokens, rate).max(1) as usize,
    )
}

fn tokens_of_span(ct: &CharText, span: (usize, usize), rate: f64) -> i64 {
    approx_tokens(ct.slice(span.0, span.1), Some(rate))
}
