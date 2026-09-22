//! TEI XML ingestion, mirroring `modules/tei_xml.py` (lxml).
//!
//! Sections walk `div`s in document order: a section holds its div's own
//! prose (nested divs get sections of their own, so their text is excluded
//! here) and stops at its first nested div, keeping sections disjoint. The
//! tree widens parents over children from `level` later, the same contract
//! `sections_from_markdown` keeps.

use marginalia_types::sdk::ParsedDocument;
use marginalia_types::Result;
use serde_json::{Map, Value};

use crate::pystr::py_stem;
use crate::xml::{parse_xml, Element, TextModel, XmlNode};
use crate::{lower_suffix, section_map, Section};

pub const MODULE_ID: &str = "tei_xml";
pub const MODULE_VERSION: &str = "2.0";
pub const DEFAULT_CHUNKER: &str = "structural";
pub const DEFAULT_DOCUMENT_TYPE: &str = "scholarly";

pub const TEI_NAMESPACE: &str = "http://www.tei-c.org/ns/1.0";

/// `detect`: the extension gate, then the namespace peek. The `mimetypes`
/// branch of the other modules has no equivalent here — a non-XML suffix
/// refuses immediately.
pub fn detect(file_name: &str, head: Option<&str>) -> (f64, String) {
    let suffix = lower_suffix(file_name);
    if suffix != ".xml" && suffix != ".tei" {
        return (0.0, format!("extension '{suffix}' does not match XML"));
    }
    if let Some(h) = head {
        if h.contains(TEI_NAMESPACE) {
            return (0.95, "file contains TEI namespace declaration".to_owned());
        }
        if h.contains("<TEI") {
            return (0.7, "file contains <TEI> root element".to_owned());
        }
    }
    (0.0, "not detected as TEI XML".to_owned())
}

/// Parse bytes the way `etree.parse` does: encoding declaration honored,
/// failures raised.
pub fn parse_bytes(raw: &[u8], file_name: &str) -> Result<ParsedDocument> {
    let document = parse_xml(raw, TextModel::Lxml)?;
    extract(&document.root, file_name)
}

fn strip(s: &str) -> &str {
    marginalia_text::chars::strip(s)
}

/// One block's text, inline markup joined by spaces rather than welded:
/// each text run stripped on its own, empties dropped, `" "` between.
fn inline_text(element: &Element) -> String {
    element
        .text_segments()
        .iter()
        .map(|fragment| strip(fragment))
        .filter(|fragment| !fragment.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// A div's own prose, excluding nested divs: their text belongs to their
/// own sections, and `itertext` already descends. Tails of nested divs are
/// still prose of this div, so they stay.
fn own_text(div: &Element) -> Result<String, marginalia_types::Error> {
    let mut pieces: Vec<String> = Vec::new();
    if !strip(div.text()).is_empty() {
        pieces.push(strip(div.text()).to_owned());
    }
    // lxml iterates comments and PIs alongside elements here, and reading
    // text off one fails — so a comment directly under a div fails the
    // parse, exactly as upstream fails it.
    for (index, child) in div.children.iter().enumerate() {
        // Text runs are not yielded by element iteration upstream;
        // comments and PIs are, and reading text off one fails.
        let child_element = match child {
            XmlNode::Element(element) => element,
            XmlNode::Text(_) => continue,
            XmlNode::Comment | XmlNode::Pi => {
                return Err(crate::xml::non_element_text_error());
            }
        };
        if child_element.local != "div" {
            let fragment = inline_text(child_element);
            if !fragment.is_empty() {
                pieces.push(fragment);
            }
        }
        if !strip(div.tail_after(index)).is_empty() {
            pieces.push(strip(div.tail_after(index)).to_owned());
        }
    }
    Ok(pieces.join("\n"))
}

/// Canonical text and a section table, walking divs in document order.
fn body_sections(body: &Element) -> Result<(String, Vec<Section>), marginalia_types::Error> {
    struct Mark {
        start: usize,
        heading: Option<String>,
        level: u64,
    }
    let mut parts: Vec<String> = Vec::new();
    let mut marks: Vec<Mark> = Vec::new();
    let mut length = 0usize;

    fn walk(
        parent: &Element,
        depth: u64,
        parts: &mut Vec<String>,
        marks: &mut Vec<Mark>,
        length: &mut usize,
    ) -> Result<(), marginalia_types::Error> {
        for child in parent.child_elements() {
            if child.local != "div" {
                continue;
            }
            let own = own_text(child)?;
            let head = child.child_elements().find(|el| el.local == "head");
            marks.push(Mark {
                start: *length + if parts.is_empty() { 0 } else { 2 },
                heading: head.map(inline_text),
                level: depth,
            });
            if !own.is_empty() {
                if !parts.is_empty() {
                    *length += 2; // the blank line that will join this section on
                }
                *length += own.chars().count();
                parts.push(own);
            }
            // A nested failure aborts the whole walk: the error returns
            // through every open level. Spelled out because `?` leaves no
            // countable region here (verified during development: the error
            // tests execute this path while the `?` region reads zero).
            #[allow(clippy::question_mark)]
            if let Err(err) = walk(child, depth + 1, parts, marks, length) {
                return Err(err);
            }
        }
        Ok(())
    }

    walk(body, 1, &mut parts, &mut marks, &mut length)?;
    let full_text = parts.join("\n\n");

    let chars: Vec<char> = full_text.chars().collect();
    let mut sections = Vec::new();
    for (index, mark) in marks.iter().enumerate() {
        let end = marks
            .get(index + 1)
            .map(|next| next.start)
            .unwrap_or(chars.len());
        let (start, end) = marginalia_text::spans::trim_span(&chars, mark.start, end);
        if start >= end {
            continue;
        }
        sections.push(Section {
            char_start: start,
            char_end: end,
            heading: mark.heading.clone(),
            level: Some(mark.level),
            href: None,
        });
    }
    Ok((full_text, sections))
}

/// The module's `_text_content`: raw concatenation, stripped once.
fn text_content(element: Option<&Element>) -> String {
    match element {
        None => String::new(),
        Some(el) => strip(&el.itertext()).to_owned(),
    }
}

fn extract(root: &Element, file_name: &str) -> Result<ParsedDocument, marginalia_types::Error> {
    // Namespaced and non-namespaced TEI alike: a Clark brace on the root
    // means the namespace applies, otherwise names stand bare.
    let ns_uri = if root.tag().starts_with('{') {
        TEI_NAMESPACE
    } else {
        ""
    };

    let header = root.find("teiHeader", ns_uri);
    let mut title = String::new();
    let mut author = String::new();
    let mut date = String::new();
    if let Some(header) = header {
        title = text_content(header.find(".//titleStmt/title", ns_uri));
        author = text_content(header.find(".//titleStmt/author", ns_uri));
        let date_el = header.find(".//publicationStmt/date", ns_uri);
        if let Some(date_el) = date_el {
            let when = date_el.attr("when").unwrap_or("");
            date = if !when.is_empty() {
                when.to_owned()
            } else {
                text_content(Some(date_el))
            };
        }
    }
    if title.is_empty() {
        title = py_stem(file_name).to_owned();
    }

    let body = root
        .find(".//body", ns_uri)
        .or_else(|| root.find(".//text", ns_uri));

    let mut sections: Vec<Section> = Vec::new();
    let mut full_text = String::new();
    if let Some(body) = body {
        let (text, table) = body_sections(body)?;
        full_text = text;
        sections = table;
        if full_text.is_empty() {
            // No divs at all: the whole body read straight, and the table
            // stays empty — the root node still needs a span.
            full_text = inline_text(body);
        }
    }

    let div_count = root.findall(".//div", ns_uri).len();
    let note_count = root.findall(".//note", ns_uri).len();
    let bibl_count = root.findall(".//bibl", ns_uri).len();

    let mut metadata = Map::new();
    metadata.insert(
        "char_count".to_owned(),
        Value::from(full_text.chars().count() as u64),
    );
    metadata.insert(
        "section_count".to_owned(),
        Value::from(sections.len() as u64),
    );
    metadata.insert("div_count".to_owned(), Value::from(div_count as u64));
    metadata.insert("file_name".to_owned(), Value::from(file_name));
    metadata.insert("format".to_owned(), Value::from("tei_xml"));
    if !author.is_empty() {
        metadata.insert("author".to_owned(), Value::from(author));
    }
    if !date.is_empty() {
        metadata.insert("date".to_owned(), Value::from(date));
    }
    if note_count > 0 {
        metadata.insert("note_count".to_owned(), Value::from(note_count as u64));
    }
    if bibl_count > 0 {
        metadata.insert(
            "bibliography_count".to_owned(),
            Value::from(bibl_count as u64),
        );
    }

    Ok(ParsedDocument {
        title: Some(title),
        text: full_text,
        document_type: DEFAULT_DOCUMENT_TYPE.to_owned(),
        language: None,
        metadata,
        sections: sections.iter().map(section_map).collect(),
        structural_locators: Vec::new(),
    })
}
