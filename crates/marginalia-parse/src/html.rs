//! HTML ingestion, mirroring `modules/html.py` (BeautifulSoup, `html.parser`).
//!
//! The tree comes from html5ever (via `scraper`); the walk replays
//! `_text_and_sections` exactly: a pre-order pass over the body's (or, with
//! no `<body>` in source, the whole document's) descendants, heading marks
//! opened where the next piece will start, exact-string text only —
//! comments, doctypes, and processing instructions are separate node kinds
//! on both sides and never count — stripped pieces joined with `"\n"`.
//!
//! Two `html.parser` behaviors need explicit care:
//! - It never synthesizes `<html>`/`<head>`/`<body>`, so a fragment without
//!   a body tag is read whole (head text included). html5ever always builds
//!   the implied structure, so [`has_explicit_body`] re-reads the source
//!   for a real body start tag first.
//! - `<title>` is raw text on both sides (tags inside it are text, not
//!   elements), so `.string` is the single text child when there is exactly
//!   one — including a lone comment, which subclasses `NavigableString`.

use marginalia_types::sdk::ParsedDocument;
use marginalia_types::Result;
use scraper::{Html, Selector};
use serde_json::{Map, Value};

use crate::{decode_replace, lower_suffix, Section};

pub const MODULE_ID: &str = "html";
pub const MODULE_VERSION: &str = "1.0";
pub const DEFAULT_CHUNKER: &str = "structural";
pub const DEFAULT_DOCUMENT_TYPE: &str = "generic";

const HEADINGS: [&str; 6] = ["h1", "h2", "h3", "h4", "h5", "h6"];
/// Subtrees that never contribute text. `template` counts as one: its
/// contents are opaque to traversal on both sides (a fragment node here, no
/// descendants there), while counts still see them.
const PRUNE: [&str; 4] = ["script", "style", "noscript", "template"];

/// `detect`: the extension branch, then the marker peek. The `mimetypes`
/// branch between them reads the OS table and stays caller-side.
pub fn detect(file_name: &str, head: Option<&str>) -> (f64, String) {
    let suffix = lower_suffix(file_name);
    if suffix == ".html" || suffix == ".htm" {
        return (0.9, format!("extension '{suffix}' matches HTML"));
    }
    if let Some(h) = head {
        let lower = h.to_lowercase();
        if lower.contains("<html") || lower.contains("<!doctype html") {
            return (0.7, "file contains HTML markers".to_owned());
        }
    }
    (0.0, "not detected as HTML".to_owned())
}

/// Parse bytes read with `errors="replace"` in text mode: replacement
/// for bad bytes, universal newlines for line endings.
pub fn parse_bytes(raw: &[u8], file_name: &str) -> Result<ParsedDocument> {
    parse_text(&crate::translate_newlines(&decode_replace(raw)), file_name)
}

/// Whether the source carries a real `<body>` start tag — the condition
/// under which `soup.find("body")` finds anything at all.
///
/// Comments, declarations, processing instructions, and the raw-text
/// elements (`script`, `style`, `title`, `textarea`) cannot open one, which
/// is what the scan skips. Everything else is compared case-insensitively,
/// the way both tokenizers fold tag names.
pub fn has_explicit_body(source: &str) -> bool {
    let bytes = source.as_bytes();
    let len = bytes.len();
    let mut i = 0;
    while i < len {
        if bytes[i] != b'<' {
            i += 1;
            continue;
        }
        if starts_with_ignore_case(bytes, i, "<!--") {
            i = skip_to(bytes, i + 4, "-->");
            continue;
        }
        if starts_with_ignore_case(bytes, i, "<?")
            || (starts_with_ignore_case(bytes, i, "<!")
                && !starts_with_ignore_case(bytes, i, "<!--"))
        {
            i = skip_to(bytes, i + 2, ">");
            continue;
        }
        if starts_with_ignore_case(bytes, i, "</") {
            i = skip_to(bytes, i + 2, ">");
            continue;
        }
        // An open tag: read its name, then skip quoted attributes.
        let mut j = i + 1;
        while j < len && is_tag_name_char(bytes[j]) {
            j += 1;
        }
        let name = &source[i + 1..j.min(len)];
        if name.eq_ignore_ascii_case("body")
            && (j >= len || matches!(bytes[j], b'/' | b'>') || bytes[j].is_ascii_whitespace())
        {
            return true;
        }
        if matches!(
            name.to_lowercase().as_str(),
            "script" | "style" | "title" | "textarea"
        ) {
            j = skip_tag_remainder(bytes, j);
            let close = format!("</{name}");
            i = skip_to_ignore_case(bytes, j, &close);
            continue;
        }
        i = skip_tag_remainder(bytes, j);
    }
    false
}

fn starts_with_ignore_case(bytes: &[u8], at: usize, pat: &str) -> bool {
    bytes.len() >= at + pat.len() && bytes[at..at + pat.len()].eq_ignore_ascii_case(pat.as_bytes())
}

fn is_tag_name_char(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'-' | b':' | b'_')
}

fn skip_to(bytes: &[u8], mut i: usize, pat: &str) -> usize {
    let target = pat.as_bytes();
    while i + target.len() <= bytes.len() {
        if &bytes[i..i + target.len()] == target {
            return i + target.len();
        }
        i += 1;
    }
    bytes.len()
}

fn skip_to_ignore_case(bytes: &[u8], mut i: usize, pat: &str) -> usize {
    while i + pat.len() <= bytes.len() {
        if bytes[i..i + pat.len()].eq_ignore_ascii_case(pat.as_bytes()) {
            return i + pat.len();
        }
        i += 1;
    }
    bytes.len()
}

/// Skip an open tag's remainder: quoted attribute values may hold `>`.
fn skip_tag_remainder(bytes: &[u8], mut j: usize) -> usize {
    while j < bytes.len() {
        match bytes[j] {
            b'"' | b'\'' => {
                let quote = bytes[j];
                j += 1;
                while j < bytes.len() && bytes[j] != quote {
                    j += 1;
                }
                j += 1;
            }
            b'>' => return j + 1,
            _ => j += 1,
        }
    }
    bytes.len()
}

/// Collect an element's descendant text the way `get_text(separator, True)`
/// does: exact-string nodes only, stripped, empties dropped, joined with
/// `separator`. Pruned subtrees never contribute.
fn subtree_text(element: scraper::ElementRef<'_>, separator: &str) -> String {
    let mut pieces: Vec<&str> = Vec::new();
    collect_text(element, &mut pieces);
    pieces.join(separator)
}

fn collect_text<'a>(element: scraper::ElementRef<'a>, pieces: &mut Vec<&'a str>) {
    for child in element.children() {
        match child.value() {
            scraper::Node::Text(text) => {
                let stripped = marginalia_text::chars::strip(text);
                // Whitespace-only runs between blocks never reach the text:
                // the module strips and skips them before joining.
                pieces.extend(std::iter::once(stripped).filter(|s| !s.is_empty()));
            }
            scraper::Node::Element(el) => {
                if PRUNE.contains(&el.name()) {
                    continue;
                }
                // Matched as an element node above; wrapping it as an
                // element reference cannot fail.
                let next = scraper::ElementRef::wrap(child).expect("element node wraps");
                collect_text(next, pieces);
            }
            _ => {}
        }
    }
}

/// `Tag.string` for a `title` element: its single text child, if there is
/// exactly one. Titles parse as raw text on both sides — tags, comments,
/// and processing instructions inside one arrive as text, never as nodes
/// (verified: `<!--hidden-->` and `A <b>Page</b>` each arrive whole) — so
/// the comment, element, and empty arms `Tag.string` carries never fire.
/// The single-string-child contract `tag_string` keeps, exposed for its
/// own test: titles only ever present the text shape (raw-text model).
pub fn tag_string(element: scraper::ElementRef<'_>) -> Option<String> {
    let children: Vec<_> = element.children().collect();
    if children.len() != 1 {
        return None;
    }
    match children[0].value() {
        scraper::Node::Text(text) => {
            let s: &str = text;
            Some(s.to_owned())
        }
        _ => None,
    }
}

/// The document's text plus where each heading sits inside it.
///
/// Replays `get_text(separator="\n", strip=True)` piece for piece rather
/// than approximating it: offsets are only worth anything against the
/// string actually stored. `descendants` is pre-order, so a heading arrives
/// before its strings and its mark opens where the next piece will start.
pub fn text_and_sections(target: scraper::ElementRef<'_>) -> (String, Vec<Section>) {
    let mut parts: Vec<String> = Vec::new();
    let mut length = 0usize;
    let mut marks: Vec<(usize, String, u64)> = Vec::new();
    walk(target, &mut parts, &mut length, &mut marks);
    let full_text = parts.join("\n");
    let chars: Vec<char> = full_text.chars().collect();
    let mut sections = Vec::new();
    for (index, (start, heading, level)) in marks.iter().enumerate() {
        let end = marks.get(index + 1).map(|m| m.0).unwrap_or(chars.len());
        let (start, end) = marginalia_text::spans::trim_span(&chars, *start, end);
        if start >= end {
            continue;
        }
        sections.push(Section {
            char_start: start,
            char_end: end,
            heading: Some(heading.clone()),
            level: Some(*level),
            href: None,
        });
    }
    (full_text, sections)
}

fn walk(
    element: scraper::ElementRef<'_>,
    parts: &mut Vec<String>,
    length: &mut usize,
    marks: &mut Vec<(usize, String, u64)>,
) {
    for child in element.children() {
        match child.value() {
            scraper::Node::Element(el) => {
                if PRUNE.contains(&el.name()) {
                    continue;
                }
                if let Some(level) = HEADINGS.iter().position(|h| *h == el.name()) {
                    // Matched as an element node above; wrapping it as an
                    // element reference cannot fail.
                    let next = scraper::ElementRef::wrap(child).expect("element node wraps");
                    marks.push((
                        *length + usize::from(!parts.is_empty()),
                        subtree_text(next, " "),
                        level as u64 + 1,
                    ));
                    walk(next, parts, length, marks);
                    continue;
                }
                // Matched as an element node above; wrapping it as an
                // element reference cannot fail.
                let next = scraper::ElementRef::wrap(child).expect("element node wraps");
                walk(next, parts, length, marks);
            }
            scraper::Node::Text(text) => {
                let stripped = marginalia_text::chars::strip(text);
                if stripped.is_empty() {
                    continue;
                }
                if !parts.is_empty() {
                    *length += 1; // the separator that will join this piece on
                }
                *length += stripped.chars().count();
                parts.push(stripped.to_owned());
            }
            _ => {}
        }
    }
}

/// Parse already-decoded text.
pub fn parse_text(text: &str, file_name: &str) -> Result<ParsedDocument, marginalia_types::Error> {
    // Entity spellings the two tokenizers read differently are normalized
    // first. The reconciler is infallible (every shape resolves to a
    // spelling), so this unwraps rather than propagates.
    let normalized = crate::normalize_entities::normalize_entities(text)
        .expect("entity reconciliation is infallible");
    let text = normalized.as_str();
    let html = Html::parse_document(text);
    let title_selector = Selector::parse("title").unwrap();
    let h1_selector = Selector::parse("h1").unwrap();
    let body_selector = Selector::parse("body").unwrap();
    let html_selector = Selector::parse("html").unwrap();
    let meta_selector = Selector::parse("meta").unwrap();
    let heading_selector = Selector::parse("h1,h2,h3,h4,h5,h6").unwrap();
    let link_selector = Selector::parse("a[href]").unwrap();

    let title = match html.select(&title_selector).next().and_then(tag_string) {
        Some(s) if !s.is_empty() => marginalia_text::chars::strip(&s).to_owned(),
        _ => match html.select(&h1_selector).next() {
            Some(h1) => subtree_text(h1, ""),
            None => crate::pystr::py_stem(file_name).to_owned(),
        },
    };

    // `Html::parse_document` always synthesizes html/head/body, so a body
    // element is present even for fragments and empty input: the missing
    // branch cannot happen, with or without an explicit tag in source.
    let body = has_explicit_body(text)
        .then(|| html.select(&body_selector).next())
        .flatten()
        .unwrap_or_else(|| html.root_element());
    let (full_text, sections) = text_and_sections(body);

    let mut meta_description = String::new();
    let mut meta_author = String::new();
    let mut meta_language = String::new();
    for meta in html.select(&meta_selector) {
        let el = meta.value();
        let name = el
            .attr("name")
            .or_else(|| el.attr("property"))
            .unwrap_or("")
            .to_lowercase();
        let content = el.attr("content").unwrap_or("").to_owned();
        match name.as_str() {
            "description" => meta_description = content,
            "author" => meta_author = content,
            "language" => meta_language = content,
            _ => {}
        }
    }
    if meta_language.is_empty() {
        // The element always synthesizes; only the attribute may miss
        // (present-but-empty reads as empty, exactly as upstream reads it).
        let html_el = html
            .select(&html_selector)
            .next()
            .expect("the html element always synthesizes");
        meta_language = html_el.value().attr("lang").unwrap_or("").to_owned();
    }

    let heading_count = html.select(&heading_selector).count();
    let link_count = html.select(&link_selector).count();

    let mut metadata = Map::new();
    metadata.insert(
        "char_count".to_owned(),
        Value::from(full_text.chars().count() as u64),
    );
    metadata.insert(
        "heading_count".to_owned(),
        Value::from(heading_count as u64),
    );
    metadata.insert("link_count".to_owned(), Value::from(link_count as u64));
    metadata.insert("file_name".to_owned(), Value::from(file_name));
    if !meta_description.is_empty() {
        metadata.insert("description".to_owned(), Value::from(meta_description));
    }
    if !meta_author.is_empty() {
        metadata.insert("author".to_owned(), Value::from(meta_author));
    }
    if !meta_language.is_empty() {
        metadata.insert("language".to_owned(), Value::from(meta_language.clone()));
    }

    Ok(ParsedDocument {
        title: Some(title),
        text: full_text,
        document_type: DEFAULT_DOCUMENT_TYPE.to_owned(),
        language: if meta_language.is_empty() {
            None
        } else {
            Some(meta_language)
        },
        metadata,
        sections: sections.iter().map(crate::section_map).collect(),
        structural_locators: Vec::new(),
    })
}
