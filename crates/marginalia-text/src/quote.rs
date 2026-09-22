//! Quote verification decision core, mirroring
//! `services/verification/quote.py` minus its repositories.
//!
//! The DB orchestration (candidate loop, passage/node resolution) stays Python
//! until Phase 5. Everything here runs on in-memory text with identical tier
//! boundaries: `exact` is character-for-character, `normalized` matched only
//! after typographic folding, `near` reports where the quotation diverges.
//!
//! Character offsets throughout, exactly as Python string indices are.
//!
//! Two totality notes where Python is partial:
//! - An empty `match_form` (a quotation folding to nothing) raises
//!   `IndexError` in `_find_folded`; here it matches nothing and falls
//!   through to `not_found`.
//! - `matched_fraction` rounds half away from zero where Python's `round()`
//!   rounds half to even. The two agree unless the fraction is an exact
//!   thousandth tie, which `prefix_len / len` essentially never is.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::anchoring::Span;
use crate::chars::strip;
use crate::normalize::{normalize, normalize_for_matching};

/// Fraction of a quotation that must match before a miss is worth reporting
/// as a near miss rather than a plain absence.
pub const DEFAULT_NEAR_THRESHOLD: f64 = 0.5;

/// How many documents a corpus-wide check will open.
pub const MAX_CANDIDATES: usize = 10;

/// Characters of context shown either side of where a near miss diverges.
pub const DIVERGENCE_CONTEXT: usize = 80;

pub const EXACT_DETAIL: &str = "The source contains this quotation character for character.";
pub const NORMALIZED_DETAIL: &str = "The source contains this quotation apart from typography \
    — whitespace, quote marks, dashes or hyphenation. Compare \
    `source_text` before quoting it verbatim.";
const EMPTY_DETAIL: &str = "The quotation is empty.";
const NOT_FOUND_DETAIL: &str = "No document contains this quotation, and no substantial \
    part of it either. Check the wording, or the document may \
    not be in the corpus.";

/// How closely the source matched, from the caller's point of view.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Tier {
    Exact,
    Normalized,
    Near,
    NotFound,
    NoCanonicalText,
}

/// The innermost recorded structure containing a quotation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QuoteNode {
    pub id: Uuid,
    pub node_type: String,
    pub title: Option<String>,
    pub path: String,
    pub char_start: usize,
    pub char_end: usize,
}

/// Where a verified quotation sits in its source.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QuoteLocation {
    pub document_id: Uuid,
    pub document_title: Option<String>,
    pub char_start: usize,
    pub char_end: usize,
    /// What the document actually says at this span, in raw form.
    pub source_text: String,
    #[serde(default)]
    pub passage_ids: Vec<Uuid>,
    /// Locators of the covering passages — page numbers, not the quotation.
    #[serde(default)]
    pub locators: Vec<Map<String, Value>>,
    /// The narrowest structural unit enclosing the quotation.
    #[serde(default)]
    pub node: Option<QuoteNode>,
}

impl QuoteLocation {
    pub fn straddles_passages(&self) -> bool {
        self.passage_ids.len() > 1
    }
}

/// Where a near-miss quotation stops agreeing with the source.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Divergence {
    pub matched_characters: usize,
    /// The tail of the quotation that matched, for orientation.
    pub matched_tail: String,
    /// What the quotation says next.
    pub quote_continues: String,
    /// What the source says next instead.
    pub source_continues: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QuoteVerification {
    pub tier: Tier,
    pub quote: String,
    #[serde(default)]
    pub location: Option<QuoteLocation>,
    /// For `near`: how much of the quotation matched before diverging.
    #[serde(default)]
    pub matched_fraction: Option<f64>,
    #[serde(default)]
    pub divergence: Option<Divergence>,
    #[serde(default)]
    pub documents_checked: usize,
    #[serde(default)]
    pub detail: String,
}

impl QuoteVerification {
    /// True only for a match a citation can rest on.
    pub fn verified(&self) -> bool {
        matches!(self.tier, Tier::Exact | Tier::Normalized)
    }
}

/// Locate a folded needle in `raw`, returning raw offsets.
pub fn find_folded(raw: &str, match_form: &str) -> Option<Span> {
    if match_form.is_empty() {
        return None;
    }
    let (folded, index_map) = crate::normalize::normalize_with_map(raw);
    let folded: Vec<char> = folded.chars().collect();
    let needle: Vec<char> = match_form.chars().collect();
    let at = find_subslice(&folded, &needle)?;
    Some(Span {
        start: index_map[at],
        end: index_map[at + needle.len() - 1] + 1,
    })
}

/// Try the caller's neighbourhood before the whole document.
///
/// Returns document-addressed spans (`lo`-rebased), exactly as the Python
/// `_locate_in_window` does.
pub fn locate_in_window(
    raw: &str,
    window: (usize, usize),
    quote: &str,
    match_form: &str,
) -> Option<(Tier, Span)> {
    let raw_chars: Vec<char> = raw.chars().collect();
    let (start, end) = window;
    let slack = quote.chars().count() + 256;
    let lo = start.saturating_sub(slack);
    let hi = (end + slack).min(raw_chars.len());
    let window_text: String = raw_chars[lo..hi].iter().collect();
    let window_chars: Vec<char> = raw_chars[lo..hi].to_vec();
    let quote_chars: Vec<char> = quote.chars().collect();
    if let Some(at) = find_subslice(&window_chars, &quote_chars) {
        return Some((
            Tier::Exact,
            Span {
                start: lo + at,
                end: lo + at + quote_chars.len(),
            },
        ));
    }
    find_folded(&window_text, match_form).map(|found| {
        (
            Tier::Normalized,
            Span {
                start: lo + found.start,
                end: lo + found.end,
            },
        )
    })
}

/// Find a typographically-folded quotation in raw offsets, windowed.
///
/// The raw:normalized length ratio estimates where the match sits and only
/// that window is folded; widening slacks then the whole-document fallback
/// exist because "almost always" is not a correctness argument.
pub fn locate_normalized_windowed(
    raw: &str,
    norm_offset: usize,
    raw_len: usize,
    norm_len: usize,
    match_form: &str,
) -> Option<Span> {
    let raw_chars: Vec<char> = raw.chars().collect();
    let approximate = norm_offset * raw_len / norm_len.max(1);
    let span = match_form.chars().count() * 2;

    for slack in [4_096_usize, 65_536, 1_048_576] {
        let lo = approximate.saturating_sub(slack);
        let hi = (approximate + span + slack).min(raw_len);
        let window: String = raw_chars[lo..hi].iter().collect();
        if let Some(found) = find_folded(&window, match_form) {
            return Some(Span {
                start: lo + found.start,
                end: lo + found.end,
            });
        }
        if lo == 0 && hi == raw_len {
            return None; // the window was already the whole document
        }
    }
    find_folded(raw, match_form)
}

/// Binary search the longest prefix of `stored` that exists.
///
/// `is_contained` answers one indexed probe. Whitespace-only probes count as
/// absent, exactly like the Python `holder_of`.
pub fn longest_prefix_len(stored: &str, is_contained: &dyn Fn(&str) -> bool) -> usize {
    let chars: Vec<char> = stored.chars().collect();
    let holder_of = |length: usize| -> bool {
        let prefix: String = chars[..length].iter().collect();
        if strip(&prefix).is_empty() {
            return false;
        }
        is_contained(&prefix)
    };

    // `lo` starts at 1 and only grows, so `mid` never reaches 0: no guard needed.
    let (mut best, mut lo, mut hi) = (0, 1, chars.len());
    while lo <= hi {
        let mid = (lo + hi) / 2;
        if holder_of(mid) {
            best = mid;
            lo = mid + 1;
        } else {
            hi = mid - 1;
        }
    }
    best
}

/// The full tier decision over one in-memory document.
///
/// `document_id` only labels the returned location; passage locators and the
/// structural node stay empty for the repository-backed `_resolve` to fill in
/// Phase 5.
pub fn verify_against_text(
    document_id: Uuid,
    raw: &str,
    quote: &str,
    near_threshold: f64,
) -> QuoteVerification {
    let quote = strip(quote).to_owned();
    if quote.is_empty() {
        return QuoteVerification {
            tier: Tier::NotFound,
            quote,
            location: None,
            matched_fraction: None,
            divergence: None,
            documents_checked: 0,
            detail: EMPTY_DETAIL.to_owned(),
        };
    }

    // `normalize` pairs with the stored normalized column; `match_form` pairs
    // with `normalize_with_map` output. Mixing them silently fails on
    // combining marks.
    let stored_form = normalize(&quote);
    let match_form = normalize_for_matching(&quote);
    let raw_chars: Vec<char> = raw.chars().collect();

    let quote_chars: Vec<char> = quote.chars().collect();
    if let Some(at) = find_subslice(&raw_chars, &quote_chars) {
        let span = Span {
            start: at,
            end: at + quote_chars.len(),
        };
        return resolve(
            Tier::Exact,
            &quote,
            document_id,
            raw,
            &span,
            1,
            EXACT_DETAIL,
        );
    }

    let normalized_doc = normalize(raw);
    let raw_len = raw_chars.len();
    let norm_len = normalized_doc.chars().count();
    if !stored_form.is_empty() {
        if let Some(norm_offset) = char_find(&normalized_doc, &stored_form) {
            if let Some(span) =
                locate_normalized_windowed(raw, norm_offset, raw_len, norm_len, &match_form)
            {
                return resolve(
                    Tier::Normalized,
                    &quote,
                    document_id,
                    raw,
                    &span,
                    1,
                    NORMALIZED_DETAIL,
                );
            }
        }
    }

    near_miss(
        document_id,
        raw,
        &quote,
        &stored_form,
        &normalized_doc,
        near_threshold,
    )
}

fn near_miss(
    document_id: Uuid,
    raw: &str,
    quote: &str,
    stored_form: &str,
    normalized_doc: &str,
    near_threshold: f64,
) -> QuoteVerification {
    let prefix_len = longest_prefix_len(stored_form, &|p| normalized_doc.contains(p));
    let stored_chars: Vec<char> = stored_form.chars().collect();
    let fraction = if stored_chars.is_empty() {
        0.0
    } else {
        prefix_len as f64 / stored_chars.len() as f64
    };

    if fraction < near_threshold {
        return QuoteVerification {
            tier: Tier::NotFound,
            quote: quote.to_owned(),
            location: None,
            matched_fraction: if stored_chars.is_empty() {
                None
            } else {
                Some(round3(fraction))
            },
            divergence: None,
            documents_checked: 0,
            detail: NOT_FOUND_DETAIL.to_owned(),
        };
    }

    let matched_prefix: String = stored_chars[..prefix_len].iter().collect();
    let match_prefix_form = normalize_for_matching(&matched_prefix);
    let span = char_find(normalized_doc, &matched_prefix).and_then(|norm_offset| {
        locate_normalized_windowed(
            raw,
            norm_offset,
            raw.chars().count(),
            normalized_doc.chars().count(),
            &match_prefix_form,
        )
    });

    let raw_chars: Vec<char> = raw.chars().collect();
    let source_continues = span
        .map(|s| {
            let following: String = raw_chars
                [s.end..(s.end + DIVERGENCE_CONTEXT).min(raw_chars.len())]
                .iter()
                .collect();
            normalize(&following)
        })
        .unwrap_or_default();

    let tail_start = prefix_len.saturating_sub(DIVERGENCE_CONTEXT);
    let detail = format!(
        "The source matches the first {prefix_len} characters of this \
         quotation and then diverges. Compare `quote_continues` against \
         `source_continues`."
    );
    let quote_continues: String = stored_chars
        [prefix_len..(prefix_len + DIVERGENCE_CONTEXT).min(stored_chars.len())]
        .iter()
        .collect();
    let matched_tail: String = stored_chars[tail_start..prefix_len].iter().collect();
    let mut result = QuoteVerification {
        tier: Tier::Near,
        quote: quote.to_owned(),
        location: None,
        matched_fraction: Some(round3(fraction)),
        divergence: Some(Divergence {
            matched_characters: prefix_len,
            matched_tail,
            quote_continues,
            source_continues,
        }),
        documents_checked: 1,
        detail: detail.clone(),
    };
    if let Some(span) = span {
        result.location = Some(
            resolve(Tier::Near, quote, document_id, raw, &span, 1, &detail)
                .location
                .expect("resolve always sets a location"),
        );
    }
    result
}

fn resolve(
    tier: Tier,
    quote: &str,
    document_id: Uuid,
    raw: &str,
    span: &Span,
    checked: usize,
    detail: &str,
) -> QuoteVerification {
    let raw_chars: Vec<char> = raw.chars().collect();
    let source_text: String = raw_chars[span.start..span.end].iter().collect();
    QuoteVerification {
        tier,
        quote: quote.to_owned(),
        location: Some(QuoteLocation {
            document_id,
            document_title: None,
            char_start: span.start,
            char_end: span.end,
            source_text,
            passage_ids: vec![],
            locators: vec![],
            node: None,
        }),
        matched_fraction: None,
        divergence: None,
        documents_checked: checked,
        detail: detail.to_owned(),
    }
}

fn round3(fraction: f64) -> f64 {
    (fraction * 1000.0).round() / 1000.0
}

fn find_subslice(haystack: &[char], needle: &[char]) -> Option<usize> {
    if needle.is_empty() || needle.len() > haystack.len() {
        return None;
    }
    (0..=haystack.len() - needle.len()).find(|&at| haystack[at..at + needle.len()] == *needle)
}

fn char_find(haystack: &str, needle: &str) -> Option<usize> {
    if needle.is_empty() {
        return None;
    }
    let (h, n): (Vec<char>, Vec<char>) = (haystack.chars().collect(), needle.chars().collect());
    find_subslice(&h, &n)
}
