//! Span-preserving splitting helpers, mirroring `chunking.py`'s `trim_span`,
//! `split_at_boundary`, and `cap_spans`.
//!
//! All offsets are character offsets. Errors are [`marginalia_types::Error`]
//! validation failures, matching the Python `ValueError`s.

use marginalia_types::{Error, Result};

use crate::chars::is_space;
use crate::tokens::{chars_per_token_of, min_chars_per_token_of};

/// Trim surrounding whitespace by moving the span, never by mutating text.
pub fn trim_span(text: &[char], start: usize, end: usize) -> (usize, usize) {
    let mut start = start;
    let mut end = end.min(text.len());
    while start < end && is_space(text[start]) {
        start += 1;
    }
    while end > start && is_space(text[end - 1]) {
        end -= 1;
    }
    (start, end)
}

/// Split `[start, end)` at newlines/spaces while preserving offsets.
///
/// Blank pieces are dropped. Prefers newline cuts, then spaces, then a hard
/// cut at `max_chars` — exactly the Python loop.
pub fn split_at_boundary(
    text: &[char],
    start: usize,
    end: usize,
    max_chars: usize,
) -> Result<Vec<(usize, usize)>> {
    if max_chars < 1 {
        return Err(Error::Validation("max_chars must be positive".to_owned()));
    }
    if end < start || end > text.len() {
        return Err(Error::Validation(
            "span is outside the supplied text".to_owned(),
        ));
    }
    if end - start <= max_chars {
        return Ok(if is_blank(&text[start..end]) {
            vec![]
        } else {
            vec![(start, end)]
        });
    }
    Ok(split_inner(text, start, end, max_chars))
}

/// Split `[start, end)` for callers that already hold the checked entry's
/// contract: bounds ordered and inside `text`, `max_chars >= 1`.
///
/// The chunkers build their spans from the text they split and floor their
/// budgets through [`crate::tokens::token_budget_chars`], so routing them
/// through [`split_at_boundary`]'s `Result` would re-test what construction
/// guarantees — an unhittable `Err` arm that coverage correctly flags. The
/// `debug_assert`s state the contract where tests can see it; release builds
/// pay nothing, and any caller that breaks it fails loudly in test builds
/// rather than silently emitting a wrong span.
pub fn split_span(
    text: &[char],
    start: usize,
    end: usize,
    max_chars: usize,
) -> Vec<(usize, usize)> {
    debug_assert!(end >= start && end <= text.len());
    debug_assert!(max_chars >= 1);
    split_inner(text, start, end, max_chars)
}

/// The splitting loop over already-validated bounds.
fn split_inner(text: &[char], start: usize, end: usize, max_chars: usize) -> Vec<(usize, usize)> {
    let mut pieces = Vec::new();
    let mut cursor = start;
    while end - cursor > max_chars {
        let window_end = cursor + max_chars;
        let mut cut = rfind(&text[cursor + 1..window_end], '\n')
            .map(|at| cursor + 1 + at)
            .unwrap_or(cursor);
        if cut <= cursor {
            cut = rfind(&text[cursor + 1..window_end], ' ')
                .map(|at| cursor + 1 + at)
                .unwrap_or(cursor);
        }
        if cut <= cursor {
            cut = window_end;
        }
        pieces.push((cursor, cut));
        cursor = cut;
    }
    // Always fires: each pass sets `cursor = cut <= cursor_old + max < end`
    // (the loop ran, so `end - cursor_old > max`), hence `cursor < end`.
    // Callers guarantee `max_chars >= 1`, without which the loop could stall.
    pieces.push((cursor, end));
    pieces
        .into_iter()
        .filter(|&(s, e)| !is_blank(&text[s..e]))
        .collect()
}

/// Re-split only spans whose own script density exceeds `max_tokens`.
pub fn cap_spans(
    text: &[char],
    spans: &[(usize, usize)],
    max_tokens: i64,
) -> Result<Vec<(usize, usize)>> {
    if max_tokens < 1 {
        return Err(Error::Validation("max_tokens must be positive".to_owned()));
    }
    let mut capped = Vec::new();
    for &(start, end) in spans {
        // The slice is already decoded: `slice.len()` is the char count, and
        // the rates measure in place. The old `String` collect plus
        // `approx_tokens(&piece, None)` was exactly
        // `(len as f64 / rate) as i64`, `max(1)` — identical here, including
        // the empty span resolving to `1`.
        let slice = &text[start..end];
        let rate = chars_per_token_of(slice);
        if ((slice.len() as f64 / rate) as i64).max(1) <= max_tokens {
            capped.push((start, end));
            continue;
        }
        let budget = ((max_tokens as f64 * min_chars_per_token_of(slice)) as usize).max(1);
        // Infallible here: the slice above established in-bounds order and
        // `budget` is at least 1, the only two things the checked entry rejects.
        capped.extend(split_inner(text, start, end, budget));
    }
    Ok(capped)
}

fn is_blank(slice: &[char]) -> bool {
    !slice.iter().any(|&c| !is_space(c))
}

fn rfind(slice: &[char], needle: char) -> Option<usize> {
    slice.iter().rposition(|&c| c == needle)
}
