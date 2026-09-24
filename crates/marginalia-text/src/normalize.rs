//! Normalization for quote matching, mirroring `services/text/normalize.py`.
//!
//! All offsets are character offsets, exactly as Python string indices are.
//! `normalize_with_map` folds NFKC per character rather than whole-string:
//! whole-string NFKC recombines base+combining pairs (Hebrew pointing, Greek
//! accents) into one character with no single raw offset to point at.
//!
//! Regex fidelity notes (all swept against CPython over every code point):
//! - `\s` is spelled `[\p{White_Space}\p{Z}\x1c\x1d\x1e\x1f]`: the `regex`
//!   crate's `\s` misses U+001C–U+001F that Python's matches.
//! - `(\w)` cannot be spelled at all (1,710 BMP code points differ), so the
//!   linebreak pattern matches `(\S)` and filters group 1 through the baked
//!   [`crate::word_table::is_word_char`], rescanning past rejections exactly
//!   as the regex engine advances past a failed position.

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use unicode_normalization::UnicodeNormalization;

use crate::chars::{is_space, strip};
use crate::word_table::is_word_char;

/// Bump whenever the output of [`normalize`] changes for some input.
pub const NORMALIZATION_VERSION: &str = "1.0";

/// Python `re` `\s` (str), spelled out: White_Space plus Zs/Zl/Zp
/// plus U+001C-U+001F. Verified exact over every code point.
/// Python `re` `\\s` (str) inner class, shared with section patterns.
pub const PY_WS_CLASS: &str = r"\p{White_Space}\p{Z}\x1c\x1d\x1e\x1f";
const PY_WS: &str = PY_WS_CLASS;

const SOFT_HYPHEN: char = '\u{ad}';

static WHITESPACE_RUNS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"[{PY_WS}]+")).unwrap());

/// Hyphen at a line break: "fis-\ncal" -> "fiscal". Only when the next line
/// starts lowercase, so "Anglo-\nSaxon" keeps its hyphen.
///
/// `(\S)` pre-filters; group 1 is checked against [`is_word_char`] during the
/// scan because no property expression reproduces Python `\w` exactly.
static LINEBREAK_HYPHEN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(r"(\S)[-‐‑][{PY_WS}]*\n[{PY_WS}]*([a-zß-öø-ÿ])")).unwrap()
});
fn fold_quote(c: char) -> char {
    match c {
        '‘' | '’' | '‚' | '‛' | '‹' | '›' | 'ʼ' | '′' => '\'',
        '“' | '”' | '„' | '‟' | '«' | '»' | '″' => '"',
        _ => c,
    }
}

fn fold_dash(c: char) -> char {
    match c {
        '‐' | '‑' | '‒' | '–' | '—' | '―' | '−' => '-',
        _ => c,
    }
}

/// Fold away the differences that separate a quotation from its source.
pub fn normalize(text: &str) -> String {
    let text: String = text.nfkc().collect();
    let text = text.replace(SOFT_HYPHEN, "");
    let text = replace_linebreak_hyphens(&text);
    let text: String = text.chars().map(|c| fold_dash(fold_quote(c))).collect();
    let text = WHITESPACE_RUNS.replace_all(&text, " ");
    strip(&text).to_owned()
}

/// [`normalize`], plus a map from each output character to its raw offset.
///
/// `index_map[i]` is the character offset in `text` of `out[i]`.
pub fn normalize_with_map(text: &str) -> (String, Vec<usize>) {
    let deleted = linebreak_hyphen_deletions(text);
    let mut out = String::new();
    let mut index_map: Vec<usize> = Vec::new();
    let mut in_whitespace = false;

    for (i, ch) in text.chars().enumerate() {
        if deleted.contains(&i) || ch == SOFT_HYPHEN {
            continue;
        }
        if is_space(ch) {
            if !in_whitespace && !out.is_empty() {
                out.push(' ');
                index_map.push(i);
                in_whitespace = true;
            }
            continue;
        }
        in_whitespace = false;
        for folded_ch in fold_dash(fold_quote(ch)).nfkc() {
            out.push(folded_ch);
            index_map.push(i);
        }
    }

    // Leading whitespace is never emitted (the `!out.is_empty()` guard);
    // trailing can be.
    while out.ends_with(' ') {
        out.pop();
        index_map.pop();
    }
    (out, index_map)
}

/// The query-side counterpart of [`normalize_with_map`].
pub fn normalize_for_matching(text: &str) -> String {
    normalize_with_map(text).0
}

/// One accepted linebreak-hyphen match, in byte offsets.
struct LbMatch {
    start_b: usize,
    end_b: usize,
    g1_end_b: usize,
    g2_start_b: usize,
}

/// The regex's leftmost matches whose group 1 is a Python word character.
///
/// `(\S)` over-matches `(\w)`; rejections rescan from the next character,
/// exactly as the engine advances past a failed position. Non-overlapping,
/// leftmost-first — the same iteration `finditer` performs.
fn linebreak_matches(text: &str) -> Vec<LbMatch> {
    let mut out = Vec::new();
    let mut pos = 0;
    while let Some(caps) = LINEBREAK_HYPHEN.captures_at(text, pos) {
        let m = caps.get(0).unwrap();
        let g1 = caps.get(1).unwrap();
        let g1_text = &text[g1.start()..g1.end()];
        // `(\S)` matches exactly one character.
        if !g1_text.chars().next().is_some_and(is_word_char) {
            pos = m.start() + g1_text.len();
            continue;
        }
        out.push(LbMatch {
            start_b: m.start(),
            end_b: m.end(),
            g1_end_b: g1.end(),
            g2_start_b: caps.get(2).unwrap().start(),
        });
        pos = m.end();
    }
    out
}

/// `re.sub(r"\1\2")` over [`linebreak_matches`].
fn replace_linebreak_hyphens(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    for m in linebreak_matches(text) {
        out.push_str(&text[pos..m.start_b]);
        out.push_str(&text[m.start_b..m.g1_end_b]);
        out.push_str(&text[m.g2_start_b..m.end_b]);
        pos = m.end_b;
    }
    out.push_str(&text[pos..]);
    out
}

/// Raw offsets removed by de-hyphenating across a line break.
///
/// The substitution keeps the two captured characters, so everything strictly
/// between them disappears. `(\w)` and the lowercase class each match exactly
/// one character, matching `range(match.start()+1, match.end()-1)`.
///
/// The regex reports byte offsets and the map needs character offsets. The
/// offsets only ever increase, so one running count converts them all —
/// linear in the text (counting from the start per match was quadratic: a
/// hyphen-dense 1.5M-character OCR book took 3s where Python takes 0.5s).
fn linebreak_hyphen_deletions(text: &str) -> HashSet<usize> {
    let mut deleted = HashSet::new();
    let mut counted_bytes = 0;
    let mut counted_chars = 0;
    let mut char_at = |byte: usize| {
        counted_chars += text[counted_bytes..byte].chars().count();
        counted_bytes = byte;
        counted_chars
    };
    for m in linebreak_matches(text) {
        let g1_end = char_at(m.g1_end_b);
        let g2_start = char_at(m.g2_start_b);
        deleted.extend(g1_end..g2_start);
    }
    deleted
}
