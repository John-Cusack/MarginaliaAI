//! Section recovery, mirroring `services/text/sections.py`.
//!
//! Two producers, one shape: markdown headings and `Chapter N` lines both
//! yield disjoint, document-ordered sections that `build_node_tree` nests.
//! All offsets are character offsets.

use std::collections::HashMap;
use std::sync::LazyLock;

use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::chars::strip;
use crate::spans::trim_span;

/// One recovered section boundary.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Section {
    pub char_start: usize,
    pub char_end: usize,
    pub heading: String,
    pub level: usize,
}

/// ATX headings only. Setext (`===` underlines) is not emitted by Docling and
/// is ambiguous with horizontal rules and table borders.
static HEADING: LazyLock<Regex> = LazyLock::new(|| {
    // `(\S)` spelled out: the regex crate admits U+001C-U+001F here that
    // Python excludes. Same 10-range table as normalization.
    Regex::new(&format!(
        r"(?m)^(#{{1,6}})[ \t]+([^{PY_WS_CLASS}].*?)[ \t]*$",
        PY_WS_CLASS = crate::normalize::PY_WS_CLASS,
    ))
    .unwrap()
});
/// A section runs from its heading line to the next heading of any level, or
/// to the end of the text — disjoint by construction. Prose before the first
/// heading gets no section; the tree root already spans the whole text.
pub fn sections_from_markdown(text: &str) -> Vec<Section> {
    let chars: Vec<char> = text.chars().collect();
    let matches: Vec<(usize, usize, usize, String)> = HEADING
        .captures_iter(text)
        .map(|caps| {
            let m = caps.get(0).unwrap();
            let level = caps.get(1).unwrap().as_str().len();
            let heading = strip(caps.get(2).unwrap().as_str()).to_owned();
            (
                byte_to_char(text, m.start()),
                byte_to_char(text, m.end()),
                level,
                heading,
            )
        })
        .collect();
    if matches.is_empty() {
        return vec![];
    }

    let mut sections = Vec::new();
    for (index, (start_b, _, level, heading)) in matches.iter().enumerate() {
        let end_b = if index + 1 < matches.len() {
            matches[index + 1].0
        } else {
            chars.len()
        };
        // A match always contributes its `#` heading text, so trimming can
        // never empty the span; no guard needed.
        let (start, end) = trim_span(&chars, *start_b, end_b);
        sections.push(Section {
            char_start: start,
            char_end: end,
            heading: heading.clone(),
            level: *level,
        });
    }
    sections
}

/// Chapter words, in order. Roman numerals and arabic digits are separate.
fn chapter_words() -> HashMap<&'static str, usize> {
    [
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "twentyone",
        "twentytwo",
        "twentythree",
        "twentyfour",
        "twentyfive",
    ]
    .into_iter()
    .enumerate()
    .map(|(i, w)| (w, i + 1))
    .collect()
}

fn roman() -> HashMap<&'static str, usize> {
    [
        ("i", 1),
        ("ii", 2),
        ("iii", 3),
        ("iv", 4),
        ("v", 5),
        ("vi", 6),
        ("vii", 7),
        ("viii", 8),
        ("ix", 9),
        ("x", 10),
        ("xi", 11),
        ("xii", 12),
        ("xiii", 13),
        ("xiv", 14),
        ("xv", 15),
        ("xvi", 16),
        ("xvii", 17),
        ("xviii", 18),
        ("xix", 19),
        ("xx", 20),
        ("xxi", 21),
        ("xxii", 22),
        ("xxiii", 23),
        ("xxiv", 24),
        ("xxv", 25),
    ]
    .into_iter()
    .collect()
}

static CHAPTER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?m)^[ \t]*(?:CHAPTER|Chapter)[ \t]+([A-Za-z0-9]+)[ \t]*.*$").unwrap()
});

/// A heading is a short line. Past this it is a sentence that happens to
/// begin with the word.
const MAX_HEADING_CHARS: usize = 80;

/// Fewer than this is not a sequence, it is a coincidence.
const MIN_CHAPTERS: usize = 3;

/// Chapters have a book's worth of text between them — the rule separating a
/// chapter *start* from a chapter *mention*.
const MIN_MEDIAN_GAP: usize = 5_000;

/// How far through the text the last chapter must fall.
const MUST_REACH: f64 = 0.55;

fn chapter_number(token: &str) -> Option<usize> {
    let lower = token.to_lowercase();
    if lower.chars().all(|c| c.is_ascii_digit()) && !lower.is_empty() {
        return lower.parse().ok();
    }
    chapter_words()
        .get(lower.as_str())
        .copied()
        .or_else(|| roman().get(lower.as_str()).copied())
}

/// Chapter matches split into maximal ascending runs of (number, offset, line).
fn ascending_runs(text: &str) -> Vec<Vec<(usize, usize, String)>> {
    let mut hits = Vec::new();
    for caps in CHAPTER.captures_iter(text) {
        let m = caps.get(0).unwrap();
        let line = strip(m.as_str()).to_owned();
        if line.chars().count() > MAX_HEADING_CHARS {
            continue;
        }
        let Some(number) = chapter_number(caps.get(1).unwrap().as_str()) else {
            continue;
        };
        hits.push((number, byte_to_char(text, m.start()), line));
    }

    let mut runs: Vec<Vec<(usize, usize, String)>> = Vec::new();
    let mut current: Vec<(usize, usize, String)> = Vec::new();
    for hit in hits {
        if !current.is_empty() && hit.0 <= current.last().unwrap().0 {
            runs.push(std::mem::take(&mut current));
        }
        current.push(hit);
    }
    if !current.is_empty() {
        runs.push(current);
    }
    runs
}

/// The filter below takes `_ascending_runs` output directly. (Python threads
/// the runs through a rejoin step first, but its condition —
/// `run[0] == prev[-1] + 1` — contradicts the split rule that produced the
/// runs (`hit <= last` starts a new one), so it can never fire; proven by
/// exhaustive search over short sequences and by the inequality itself.)
// Callers only pass runs of length >= 3 (the filter short-circuits first).
fn median_gap(run: &[(usize, usize, String)]) -> usize {
    let mut gaps: Vec<usize> = (0..run.len() - 1)
        .map(|i| run[i + 1].1 - run[i].1)
        .collect();
    gaps.sort_unstable();
    gaps[gaps.len() / 2]
}

/// Section boundaries from `Chapter N` lines in otherwise unmarked prose.
///
/// Returns nothing rather than guessing: most flat exports correctly yield no
/// sequence at all.
pub fn sections_from_chapters(text: &str) -> Vec<Section> {
    let chars: Vec<char> = text.chars().collect();
    let mut candidates: Vec<Vec<(usize, usize, String)>> = ascending_runs(text)
        .into_iter()
        .filter(|run| run.len() >= MIN_CHAPTERS && median_gap(run) >= MIN_MEDIAN_GAP)
        .collect();
    if candidates.is_empty() {
        return vec![];
    }

    // Widest, not longest: a back-of-book index is a longer run than the real
    // chapters it lists, and spans half a percent of the text.
    candidates.sort_by_key(|run| run.last().unwrap().1 - run[0].1);
    let chosen = candidates.last().unwrap();
    if (chosen.last().unwrap().1 as f64) < MUST_REACH * chars.len() as f64 {
        return vec![];
    }

    let mut sections = Vec::new();
    for (index, (_, offset, line)) in chosen.iter().enumerate() {
        let end = if index + 1 < chosen.len() {
            chosen[index + 1].1
        } else {
            chars.len()
        };
        // The `Chapter N` line itself survives trimming, so the span is
        // never empty; no guard needed.
        let (start, end) = trim_span(&chars, *offset, end);
        sections.push(Section {
            char_start: start,
            char_end: end,
            heading: line.clone(),
            level: 1,
        });
    }
    sections
}

fn byte_to_char(text: &str, byte: usize) -> usize {
    text[..byte].chars().count()
}
