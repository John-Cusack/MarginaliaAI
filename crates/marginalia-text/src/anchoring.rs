//! Locating an old passage's text inside canonical text, mirroring
//! `services/text/anchoring.py`.
//!
//! Whitespace-run collapsing is the whole trick: `prose_window` 1.0 rebuilt
//! each chunk as `" ".join(sentences)`, so collapsing both sides makes an old
//! passage an exact substring of the canonical text.

use serde::{Deserialize, Serialize};

use crate::chars::{is_space, strip};

/// A character-offset span `[start, end)`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Span {
    pub start: usize,
    pub end: usize,
}

impl Span {
    pub fn width(&self) -> usize {
        self.end.saturating_sub(self.start)
    }

    /// Characters shared with `other`; 0 when disjoint.
    pub fn overlap(&self, other: &Span) -> usize {
        self.end
            .min(other.end)
            .saturating_sub(self.start.max(other.start))
    }
}

/// Collapse whitespace runs, keeping a map back to raw offsets.
///
/// Each run becomes a single space mapped to the run's first character.
/// Unlike `normalize_with_map`, leading runs *are* emitted.
pub fn collapse_whitespace_with_map(text: &str) -> (String, Vec<usize>) {
    let mut out = String::new();
    let mut index_map: Vec<usize> = Vec::new();
    let mut in_whitespace = false;

    for (i, ch) in text.chars().enumerate() {
        if is_space(ch) {
            if !in_whitespace {
                out.push(' ');
                index_map.push(i);
                in_whitespace = true;
            }
        } else {
            out.push(ch);
            index_map.push(i);
            in_whitespace = false;
        }
    }
    (out, index_map)
}

/// Collapse whitespace runs only.
pub fn collapse_whitespace(text: &str) -> String {
    collapse_whitespace_with_map(text).0
}

/// A document's canonical text, prepared for repeated substring lookups.
#[derive(Debug, Clone)]
pub struct CanonicalIndex {
    collapsed: Vec<char>,
    map: Vec<usize>,
}

impl CanonicalIndex {
    pub fn new(text: &str) -> Self {
        let (collapsed, map) = collapse_whitespace_with_map(text);
        Self {
            collapsed: collapsed.chars().collect(),
            map,
        }
    }

    /// Locate `needle` in the canonical text, ignoring whitespace differences.
    ///
    /// `from_offset` is a raw offset hint: search resumes from there, so a
    /// passage repeated verbatim resolves to successive occurrences. Falls
    /// back to a search from the beginning when nothing is found after it.
    pub fn find(&self, needle: &str, from_offset: usize) -> Option<Span> {
        let target: Vec<char> = strip(&collapse_whitespace(needle)).chars().collect();
        if target.is_empty() {
            return None;
        }
        let start_at = self.collapsed_offset_at_or_after(from_offset);
        let mut found = find_subslice(&self.collapsed[start_at..], &target).map(|at| at + start_at);
        if found.is_none() && start_at > 0 {
            found = find_subslice(&self.collapsed, &target);
        }
        let at = found?;
        let raw_start = self.map[at];
        let raw_end = self.map[at + target.len() - 1] + 1;
        Some(Span {
            start: raw_start,
            end: raw_end,
        })
    }

    fn collapsed_offset_at_or_after(&self, raw_offset: usize) -> usize {
        if raw_offset == 0 {
            return 0;
        }
        // partition_point: first collapsed index whose raw offset >= hint.
        self.map.partition_point(|&raw| raw < raw_offset)
    }
}

fn find_subslice(haystack: &[char], needle: &[char]) -> Option<usize> {
    if needle.is_empty() || needle.len() > haystack.len() {
        return None;
    }
    (0..=haystack.len() - needle.len()).find(|&at| haystack[at..at + needle.len()] == *needle)
}

/// The candidate sharing the most characters with `span`.
///
/// Ties break toward the candidate that starts earlier, so the choice is
/// deterministic across runs.
pub fn best_overlap<K: Clone>(span: &Span, candidates: &[(K, Span)]) -> Option<K> {
    let mut best_key: Option<K> = None;
    let mut best_score = 0;
    let mut best_start = 0;
    for (key, candidate) in candidates {
        let score = span.overlap(candidate);
        if score > best_score || (score == best_score && score > 0 && candidate.start < best_start)
        {
            best_key = Some(key.clone());
            best_score = score;
            best_start = candidate.start;
        }
    }
    best_key
}
