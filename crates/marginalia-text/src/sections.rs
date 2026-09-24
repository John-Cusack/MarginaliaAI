//! Markdown section recovery, mirroring `services/text/sections.py`'s
//! `sections_from_markdown`. All offsets are character offsets.

use std::sync::LazyLock;

use regex::Regex;

use crate::chars::strip;
use crate::spans::trim_span;

/// One recovered section boundary.
#[derive(Debug, Clone, PartialEq, Eq)]
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
///
/// Match starts arrive in increasing byte order, so their character offsets
/// come from one running count — linear in the text, however many headings
/// it holds (a per-heading count from the start was quadratic: 16s on 80k
/// headings where Python takes 0.4s).
pub fn sections_from_markdown(text: &str) -> Vec<Section> {
    let chars: Vec<char> = text.chars().collect();
    let mut counted_bytes = 0;
    let mut counted_chars = 0;
    let matches: Vec<(usize, usize, String)> = HEADING
        .captures_iter(text)
        .map(|caps| {
            let m = caps.get(0).unwrap();
            counted_chars += text[counted_bytes..m.start()].chars().count();
            counted_bytes = m.start();
            let level = caps.get(1).unwrap().as_str().len();
            let heading = strip(caps.get(2).unwrap().as_str()).to_owned();
            (counted_chars, level, heading)
        })
        .collect();

    let mut sections = Vec::with_capacity(matches.len());
    for (index, (start, level, heading)) in matches.iter().enumerate() {
        let end = matches.get(index + 1).map_or(chars.len(), |next| next.0);
        // A match always contributes its `#` heading text, so trimming can
        // never empty the span; no guard needed.
        let (start, end) = trim_span(&chars, *start, end);
        sections.push(Section {
            char_start: start,
            char_end: end,
            heading: heading.clone(),
            level: *level,
        });
    }
    sections
}
