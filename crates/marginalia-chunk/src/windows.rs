//! Choosing how much of a document to read around a search hit, mirroring
//! `services/search/windows.py`.
//!
//! Only the decision is here. `PassageWindowReader::read` fans out to the
//! node and text repositories — two queries for the whole batch — and stays
//! Python; [`build_window`] takes the slice it read back.

use marginalia_text::anchoring::Span;
use marginalia_text::spans::trim_span;
use marginalia_text::tokens::{approx_tokens, chars_per_token, token_budget_chars};
use marginalia_types::nodes::DocumentNode;
use marginalia_types::passages::{PassageWindow, WindowSource};
use uuid::Uuid;

use crate::CharText;

/// Where to read, and how that was decided.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WindowPlan {
    pub span: Span,
    pub source: WindowSource,
    /// The node that bounded the window — not always the passage's own node.
    pub node_id: Option<Uuid>,
}

/// Decide the span to read around `passage`.
///
/// `ancestors` is the chain from `get_ancestors`, in any order — it is
/// sorted here by width rather than trusted, so a malformed tree degrades to
/// a sensible window instead of an incoherent one.
///
/// Returns `None` when there is no passage span to build around. A passage
/// without offsets predates the requirement and cannot be located in the
/// canonical text at all.
pub fn choose_window(
    passage: Option<Span>,
    ancestors: &[DocumentNode],
    budget_chars: i64,
    min_chars: i64,
) -> Option<WindowPlan> {
    let passage = passage?;
    // Widest first. The documented invariant is that parents enclose
    // children, which makes this the same as root-first; sorting means we do
    // not depend on it holding for every tree ever written.
    let mut chain: Vec<&DocumentNode> = ancestors.iter().collect();
    chain.sort_by_key(|n| std::cmp::Reverse(width(n)));
    let fits: Vec<&&DocumentNode> = chain.iter().filter(|n| width(n) <= budget_chars).collect();

    if fits.is_empty() {
        // Every node is too big — the common case for TDNT articles and for
        // any passage that straddles two sections and so resolved to the
        // root. Clip to the narrowest node rather than floating free, so a
        // hit near the start of chapter 14 does not read backwards into 13.
        let bound: Option<&DocumentNode> = chain.last().copied();
        let span = centred(passage, budget_chars, bound);
        return Some(plan(span, bound, passage));
    }

    // A window has to clear the minimum *and* actually contain more than the
    // chunk. Median passages-per-node here is 1, so the deepest node
    // frequently is the chunk: it clears any minimum and expands nothing.
    let worth_reading = min_chars.max(passage.width() as i64 + 1);

    let chosen: &DocumentNode = fits[0];
    if width(chosen) >= worth_reading {
        return Some(plan(
            Span {
                start: chosen.char_start.max(0) as usize,
                end: chosen.char_end.max(0) as usize,
            },
            Some(chosen),
            passage,
        ));
    }

    // The widest node that fits adds too little to be worth reading. Climb to
    // the smallest ancestor that can hold a useful window and widen inside
    // it. Read to the budget, not merely to the threshold: structure has
    // failed to give a useful boundary, so the token budget is the only
    // meaningful limit left.
    let bound: Option<&DocumentNode> = chain.iter().find(|n| width(n) >= worth_reading).copied();
    let span = centred(passage, budget_chars, bound);
    Some(plan(span, bound, passage))
}

/// Size the read budget from the hit's own text: it is in hand, free, and it
/// is the local script mix. The same token budget is a much shorter
/// character window in Greek or Hebrew than in English. Empty text falls back
/// to the 4-chars-per-token estimate, exactly as the reader does.
pub fn window_budgets(passage_text: &str, max_tokens: i64, min_tokens: i64) -> (i64, i64) {
    if passage_text.is_empty() {
        (max_tokens * 4, min_tokens * 4)
    } else {
        let rate = chars_per_token(passage_text);
        (
            token_budget_chars(max_tokens, rate),
            token_budget_chars(min_tokens, rate),
        )
    }
}

/// Build the expanded read from the slice the reader fetched.
///
/// `raw` is the canonical text at `[plan.span.start, plan.span.end)`,
/// `None` when the document has no canonical text. The true end comes from
/// what came back, not from what was asked for: `get_span` clamps at the end
/// of the text without saying so.
pub fn build_window(
    span: Option<Span>,
    plan: &WindowPlan,
    chain: &[DocumentNode],
    raw: Option<&str>,
) -> Option<PassageWindow> {
    // None means the document has no canonical text; "" means an empty
    // slice. Neither is something to hand a reader.
    let raw = raw.filter(|r| !r.is_empty())?;
    let ct = CharText::new(raw);
    let raw_len = ct.len() as i64;
    let requested = plan.span.start as i64;

    // `trim_span` indexes into the text it is given, so it takes offsets
    // into *this slice*, not into the document. Translate afterwards.
    let (lo, hi) = trim_span(ct.chars(), 0, ct.len());
    let mut start = requested + lo as i64;
    let mut end = requested + hi as i64;

    // Trimming can eat into the passage itself when the window opens on
    // whitespace inside it, so the floor is re-applied here rather than
    // trusted from `choose_window`.
    if let Some(passage) = span {
        start = start.min((passage.start as i64).max(requested));
        end = end.max((passage.end as i64).min(requested + raw_len));
    }

    let text = ct.slice(
        (start - requested).max(0) as usize,
        (end - requested).max(0) as usize,
    );
    if text.is_empty() {
        return None;
    }
    Some(PassageWindow {
        text: text.to_owned(),
        char_start: start,
        char_end: end,
        source: plan.source,
        node_id: plan.node_id,
        breadcrumb: chain.iter().filter_map(|n| n.title.clone()).collect(),
        approx_tokens: approx_tokens(text, None),
    })
}

fn width(node: &DocumentNode) -> i64 {
    node.char_end - node.char_start
}

/// A span of `budget` centred on `passage`, clipped to `bound`.
fn centred(passage: Span, budget: i64, bound: Option<&DocumentNode>) -> Span {
    let lo = bound.map(|n| n.char_start).unwrap_or(0);
    let hi = bound.map(|n| n.char_end);

    let midpoint = (passage.start as i64 + passage.end as i64) / 2;
    // `div_euclid`, not `/`: Python `//` floors, `/` truncates — identical
    // for real (non-negative) budgets, exact for every `i64` either way.
    let mut start = midpoint - budget.div_euclid(2);
    let mut end = start + budget;

    // Slide rather than truncate when the window runs off one end, so a hit
    // near the start of a section still gets a full window's worth of context.
    if start < lo {
        start = lo;
        end = lo + budget;
    }
    if let Some(hi) = hi {
        if end > hi {
            end = hi;
            start = lo.max(hi - budget);
        }
    }
    Span {
        start: start.max(0) as usize,
        end: end.max(0) as usize,
    }
}

/// Apply the floor and name what happened: whatever the structure says,
/// never hand back less than the chunk the caller already had.
fn plan(span: Span, bound: Option<&DocumentNode>, passage: Span) -> WindowPlan {
    let span = Span {
        start: span.start.min(passage.start),
        end: span.end.max(passage.end),
    };
    if span == passage {
        return WindowPlan {
            span,
            source: WindowSource::Passage,
            node_id: bound.map(|n| n.id),
        };
    }
    let Some(bound) = bound else {
        return WindowPlan {
            span,
            source: WindowSource::DocumentWindow,
            node_id: None,
        };
    };
    if span.start as i64 == bound.char_start && span.end as i64 == bound.char_end {
        return WindowPlan {
            span,
            source: WindowSource::Node,
            node_id: Some(bound.id),
        };
    }
    WindowPlan {
        span,
        source: WindowSource::NodeWindow,
        node_id: Some(bound.id),
    }
}
