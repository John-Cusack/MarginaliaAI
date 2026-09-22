//! Markdown ingestion, mirroring `modules/markdown.py`.
//!
//! The module is regex stripping, not a Markdown grammar: formatting comes
//! out, heading markers stay in (they are what `reindex structure` reads
//! back), and the section table is recovered from the stripped text. Every
//! pattern below is the module's own, in its order; `\s` is spelled with
//! the shared Python class.
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
use marginalia_types::Result;
use regex::Regex;
use serde_json::{Map, Value};

use crate::pystr::py_stem;
use crate::{decode_strict, lower_suffix};

pub const MODULE_ID: &str = "markdown";
pub const MODULE_VERSION: &str = "2.0";
pub const DEFAULT_CHUNKER: &str = "structural";
pub const DEFAULT_DOCUMENT_TYPE: &str = "generic";

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
static HORIZONTAL_RULE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?m)^[-*_]{{3,}}{}*?$", ws())).unwrap());
static LIST_MARKER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?m)^({}*)[-*+]{ }+", ws(), ws())).unwrap());
static ORDERED_LIST: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?m)^({}*)\d+\.{ }+", ws(), ws())).unwrap());
static BLANK_RUN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\n{3,}").unwrap());
static TITLE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?m)^#{{1,6}}{}+(.+)$", ws())).unwrap());
static HEADING_PEEK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?m)^#{{1,6}}{}+", ws())).unwrap());

/// `detect`: the extension branch, then the heading peek. The `mimetypes`
/// branch between them reads the OS table and stays caller-side.
pub fn detect(file_name: &str, head: Option<&[u8]>) -> (f64, String) {
    let suffix = lower_suffix(file_name);
    if matches!(suffix.as_str(), ".md" | ".markdown" | ".mdown" | ".mkd") {
        return (0.9, format!("extension '{suffix}' matches markdown"));
    }
    // The head is read strict, the way the module opens it: undecodable
    // bytes skip the peek and score nothing.
    if let Some(raw) = head {
        if let Ok(text) = std::str::from_utf8(raw) {
            if HEADING_PEEK.is_match(text) {
                return (0.4, "file contains markdown headings".to_owned());
            }
        }
    }
    (0.0, "not detected as markdown".to_owned())
}

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

/// Parse strict-UTF-8 bytes.
pub fn parse_bytes(raw: &[u8], file_name: &str) -> Result<ParsedDocument> {
    Ok(parse_text(
        &crate::translate_newlines(decode_strict(raw)?),
        file_name,
    ))
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
