//! Markdown ingestion, mirroring `modules/markdown.py`.
//!
//! The module is regex stripping, not a Markdown grammar: formatting comes
//! out, heading markers stay in (they are what `reindex structure` reads
//! back), and the section table is recovered from the stripped text. Every
//! pattern below is the module's own, in its order; `\s` and `\d` are
//! spelled as CPython 3.13's classes, not the `regex` crate's.
//!
//! The emphasis pattern `(\*{1,3}|_{1,3})(.+?)\1` carries a backreference
//! the `regex` crate cannot express, so it is hand-rolled instead
//! ([`replace_emphasis`]): the same left-to-right scan, greedy opener
//! lengths 3→1, lazy content expansion, first end wins. A backtracking
//! engine dependency would add a failure mode (stack exhaustion) the module
//! does not have; this has none.

use std::sync::LazyLock;

use marginalia_text::normalize::PY_WS_CLASS;
use marginalia_types::sdk::ParsedDocument;
use regex::Regex;
use serde_json::{Map, Value};

use crate::py_stem;

pub const DEFAULT_DOCUMENT_TYPE: &str = "generic";

/// Python `re` `\\d` (str), baked from CPython 3.13 (Unicode 15.1.0): the
/// 64 `Nd` ranges, 680 code points. The `regex` crate's `\\d` follows its
/// own, newer Unicode tables and admits digits Python 3.13 does not.
const PY_DIGIT_CLASS: &str = concat!(
    r"\x{30}-\x{39}\x{660}-\x{669}\x{6F0}-\x{6F9}\x{7C0}-\x{7C9}\x{966}-\x{96F}",
    r"\x{9E6}-\x{9EF}\x{A66}-\x{A6F}\x{AE6}-\x{AEF}\x{B66}-\x{B6F}\x{BE6}-\x{BEF}",
    r"\x{C66}-\x{C6F}\x{CE6}-\x{CEF}\x{D66}-\x{D6F}\x{DE6}-\x{DEF}\x{E50}-\x{E59}",
    r"\x{ED0}-\x{ED9}\x{F20}-\x{F29}\x{1040}-\x{1049}\x{1090}-\x{1099}\x{17E0}-\x{17E9}",
    r"\x{1810}-\x{1819}\x{1946}-\x{194F}\x{19D0}-\x{19D9}\x{1A80}-\x{1A89}",
    r"\x{1A90}-\x{1A99}\x{1B50}-\x{1B59}\x{1BB0}-\x{1BB9}\x{1C40}-\x{1C49}",
    r"\x{1C50}-\x{1C59}\x{A620}-\x{A629}\x{A8D0}-\x{A8D9}\x{A900}-\x{A909}",
    r"\x{A9D0}-\x{A9D9}\x{A9F0}-\x{A9F9}\x{AA50}-\x{AA59}\x{ABF0}-\x{ABF9}",
    r"\x{FF10}-\x{FF19}\x{104A0}-\x{104A9}\x{10D30}-\x{10D39}\x{11066}-\x{1106F}",
    r"\x{110F0}-\x{110F9}\x{11136}-\x{1113F}\x{111D0}-\x{111D9}\x{112F0}-\x{112F9}",
    r"\x{11450}-\x{11459}\x{114D0}-\x{114D9}\x{11650}-\x{11659}\x{116C0}-\x{116C9}",
    r"\x{11730}-\x{11739}\x{118E0}-\x{118E9}\x{11950}-\x{11959}\x{11C50}-\x{11C59}",
    r"\x{11D50}-\x{11D59}\x{11DA0}-\x{11DA9}\x{11F50}-\x{11F59}\x{16A60}-\x{16A69}",
    r"\x{16AC0}-\x{16AC9}\x{16B50}-\x{16B59}\x{1D7CE}-\x{1D7FF}\x{1E140}-\x{1E149}",
    r"\x{1E2F0}-\x{1E2F9}\x{1E4F0}-\x{1E4F9}\x{1E950}-\x{1E959}\x{1FBF0}-\x{1FBF9}",
);

fn ws() -> String {
    format!("[{PY_WS_CLASS}]")
}

static CODE_BLOCK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"```[\s\S]*?```").unwrap());
static IMAGE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"!\[([^\]]*)\]\([^)]+\)").unwrap());
static LINK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\[([^\]]+)\]\([^)]+\)").unwrap());
static STRIKETHROUGH: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"~~(.+?)~~").unwrap());
static INLINE_CODE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"`([^`]+)`").unwrap());
static HTML_TAG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>]+>").unwrap());
static BLOCKQUOTE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?m)^>{}?", ws())).unwrap());
// Greedy, as Python's `\s*$`: the whitespace run may cross line ends before
// `$` settles, taking a following blank line with the rule.
static HORIZONTAL_RULE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?m)^[-*_]{{3,}}{}*$", ws())).unwrap());
static LIST_MARKER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?m)^({}*)[-*+]{ }+", ws(), ws())).unwrap());
static ORDERED_LIST: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(r"(?m)^({}*)[{PY_DIGIT_CLASS}]+\.{}+", ws(), ws())).unwrap()
});
static BLANK_RUN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\n{3,}").unwrap());
static TITLE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?m)^#{{1,6}}{}+(.+)$", ws())).unwrap());
/// `re.sub(r"(\*{1,3}|_{1,3})(.+?)\1", r"\2", text)`, by hand.
///
/// At each position an opener of 1–3 `*` (or 1–3 `_`) is tried longest
/// first; the content is the shortest run of non-`\n` characters (at least
/// one) followed by the opener again. No match emits the position's own
/// character and advances one — exactly the failed-anchor step of `re.sub`.
/// All anchors are ASCII, so every slice edge is a character boundary.
pub fn replace_emphasis(text: &str) -> String {
    let bytes = text.as_bytes();
    let len = bytes.len();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < len {
        let mark = bytes[i];
        if mark != b'*' && mark != b'_' {
            let ch = text[i..].chars().next().unwrap_or('\u{FFFD}');
            out.push(ch);
            i += ch.len_utf8();
            continue;
        }
        let mut run = 0;
        while run < 3 && i + run < len && bytes[i + run] == mark {
            run += 1;
        }
        // (content_start, closer_start, opener_len) of the first
        // backtracking success, if any.
        let mut found: Option<(usize, usize, usize)> = None;
        for opener_len in (1..=run).rev() {
            let opener = &text[i..i + opener_len];
            for (k, ch) in text[i + opener_len..].char_indices() {
                if ch == '\n' {
                    break;
                }
                let closer = i + opener_len + k;
                if closer > i + opener_len && text[closer..].starts_with(opener) {
                    found = Some((i + opener_len, closer, opener_len));
                    break;
                }
            }
            if found.is_some() {
                break;
            }
        }
        match found {
            Some((content_start, closer_start, opener_len)) => {
                out.push_str(&text[content_start..closer_start]);
                i = closer_start + opener_len;
            }
            None => {
                out.push(mark as char);
                i += 1;
            }
        }
    }
    out
}

/// Remove Markdown formatting, keeping the readable text and the headings.
/// Heading markers are deliberately absent from every pattern below.
pub fn strip_markdown(text: &str) -> String {
    let result = CODE_BLOCK.replace_all(text, "");
    let result = IMAGE.replace_all(&result, "$1");
    let result = LINK.replace_all(&result, "$1");
    let result = replace_emphasis(&result);
    let result = STRIKETHROUGH.replace_all(&result, "$1");
    let result = INLINE_CODE.replace_all(&result, "$1");
    let result = HTML_TAG.replace_all(&result, "");
    let result = BLOCKQUOTE.replace_all(&result, "");
    let result = HORIZONTAL_RULE.replace_all(&result, "");
    let result = LIST_MARKER.replace_all(&result, "$1");
    let result = ORDERED_LIST.replace_all(&result, "$1");
    let result = BLANK_RUN.replace_all(&result, "\n\n");
    marginalia_text::chars::strip(&result).to_owned()
}

/// The first `#`-heading's text, or the file stem when there is none.
pub fn extract_title(text: &str, fallback: &str) -> String {
    match TITLE.captures(text).and_then(|caps| caps.get(1)) {
        Some(m) => marginalia_text::chars::strip(m.as_str()).to_owned(),
        None => fallback.to_owned(),
    }
}

/// Parse already-decoded text: strip, title, and the section table over the
/// stripped text — the same contract EPUB's table keeps.
///
/// The section table travels in `sections` (the `ParsedDocument` field),
/// not duplicated under metadata: the pipeline reads the table, and the
/// scalars stay scalar.
pub fn parse_text(text: &str, file_name: &str) -> ParsedDocument {
    let title = extract_title(text, py_stem(file_name));
    let full_text = strip_markdown(text);
    let sections = marginalia_text::sections::sections_from_markdown(&full_text);
    let mut metadata = Map::new();
    metadata.insert(
        "char_count".to_owned(),
        Value::from(full_text.chars().count() as u64),
    );
    metadata.insert(
        "heading_count".to_owned(),
        Value::from(sections.len() as u64),
    );
    metadata.insert("file_name".to_owned(), Value::from(file_name));
    metadata.insert("format".to_owned(), Value::from("markdown"));
    let section_maps = sections
        .iter()
        .map(|s| {
            let mut map = Map::new();
            map.insert("char_start".to_owned(), Value::from(s.char_start as u64));
            map.insert("char_end".to_owned(), Value::from(s.char_end as u64));
            map.insert("heading".to_owned(), Value::from(s.heading.clone()));
            map.insert("level".to_owned(), Value::from(s.level as u64));
            map
        })
        .collect();
    ParsedDocument {
        title: Some(title),
        text: full_text,
        document_type: DEFAULT_DOCUMENT_TYPE.to_owned(),
        language: None,
        metadata,
        sections: section_maps,
        structural_locators: Vec::new(),
    }
}
