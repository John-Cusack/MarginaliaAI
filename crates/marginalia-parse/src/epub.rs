//! EPUB ingestion, mirroring `modules/epub.py` (ebooklib, `ignore_ncx=False`).
//!
//! Reading order comes from the spine, never the manifest; the navigation
//! document is apparatus, not prose; sections address the canonical text.
//! ebooklib behaviors replayed exactly:
//!
//! - The table of contents prefers the NCX (parsed only when the spine
//!   names one) and falls back to the nav document only when no NCX table
//!   exists. A nav page-list is still parsed for its errors, then dropped.
//! - `get_metadata("DC", "author")` reads a literal `author` key, which
//!   standard files never carry — so the author is usually `""`, and that
//!   is what this returns. Dublin Core text is the leading run only.
//! - Chapter HTML is read whole: no script/style removal, head text
//!   included — `get_text` over the full soup, not the body.
//! - Manifest hrefs are percent-decoded, except the nav document, whose
//!   bytes are read through the raw href. Both quirks are ebooklib's own;
//!   the offsets are only worth anything against the text the old parser
//!   stored, so they are matched rather than fixed.

use std::collections::HashMap;
use std::io::{Cursor, Read};

use marginalia_types::sdk::ParsedDocument;
use marginalia_types::{Error, Result};
use scraper::{Html, Selector};
use serde_json::{Map, Value};

use crate::pystr::py_stem;
use crate::xml::{parse_xml, Element, TextModel, XmlNode};
use crate::{decode_replace, lower_suffix, section_map, Section};

pub const MODULE_ID: &str = "epub";
pub const MODULE_VERSION: &str = "2.0";
pub const DEFAULT_CHUNKER: &str = "structural";
pub const DEFAULT_DOCUMENT_TYPE: &str = "book";

const OPF_NS: &str = "http://www.idpf.org/2007/opf";
const DAISY_NS: &str = "http://www.daisy.org/z3986/2005/ncx/";
const DC_NS: &str = "http://purl.org/dc/elements/1.1/";
const CONTAINER_NS: &str = "urn:oasis:names:tc:opendocument:xmlns:container";

/// `detect`: the extension branch, then the ZIP magic. The `mimetypes`
/// branch between them reads the OS table and stays caller-side.
pub fn detect(file_name: &str, head: &[u8]) -> (f64, String) {
    let suffix = lower_suffix(file_name);
    if suffix == ".epub" {
        return (0.9, format!("extension '{suffix}' matches EPUB"));
    }
    if head.len() >= 4 && head[..4] == *b"PK\x03\x04" {
        // Any ZIP reads this way; confidence stays low.
        return (0.2, "file is a ZIP archive (could be EPUB)".to_owned());
    }
    (0.0, "not detected as EPUB".to_owned())
}

/// Normalize chapter/nav HTML through the entity reconciler before the
/// DOM reads it. Only `html.parser`-backend text needs this; the XML
/// spine, manifest, and tables stay strict.
fn html_source(html: &str) -> Result<String> {
    crate::normalize_entities::normalize_entities(html)
}

/// Parse an `.epub` file's bytes.
pub fn parse_bytes(raw: &[u8], file_name: &str) -> Result<ParsedDocument> {
    let mut archive = zip::ZipArchive::new(Cursor::new(raw))
        .map_err(|err| Error::Parse(format!("not a ZIP archive: {err}")))?;
    let container = read_entry(&mut archive, "META-INF/container.xml")?;
    let container_doc = parse_xml(&container, TextModel::Lxml)?;
    let opf_file = find_opf(&container_doc.root)?;
    let opf_dir = posix_dirname(&opf_file).to_owned();
    let opf_bytes = read_entry(&mut archive, &opf_file)?;
    let opf_doc = parse_xml(&opf_bytes, TextModel::Lxml)?;
    let opf = &opf_doc.root;

    let dc = read_dc_metadata(opf)?;
    let manifest = read_manifest(opf, &opf_dir, &mut archive)?;
    let (spine, spine_toc) = read_spine(opf)?;

    // The NCX wins when the spine names one; the nav document only fills a
    // missing table.
    let mut toc: Vec<TocEntry> = Vec::new();
    if !spine_toc.is_empty() {
        // The bytes are already in hand: the manifest read fails first on
        // any missing file, so re-reading here could only repeat it.
        let ncx_content = manifest
            .iter()
            .find(|item| item.id == spine_toc)
            .map(|item| item.content.clone())
            .ok_or_else(|| Error::Parse("cannot find NCX file".to_owned()))?;
        toc = parse_ncx(&ncx_content)?;
    }
    if let Some(nav_item) = manifest.iter().find(|item| item.is_nav) {
        let nav = nav_item.name.clone();
        let nav_content = nav_item.content.clone();
        // The table addresses companions by manifest name: the base is the
        // nav's manifest directory, not its archive path.
        let nav_dir = posix_dirname(&nav).to_owned();
        let nav_bytes = nav_content;
        // The nav table is read through libxml2's HTML parser upstream,
        // whose entity rules are its own — reconciled separately.
        let nav_doc = Html::parse_document(&crate::normalize_entities::normalize_nav_entities(
            &decode_replace(&nav_bytes),
        ));
        if toc.is_empty() {
            toc = parse_nav_toc(&nav_doc, &nav_dir)?;
        }
        // The page list feeds nothing this module returns, but a broken one
        // fails the parse all the same.
        parse_nav_pages(&nav_doc, &nav_dir)?;
    }
    let toc_map = flatten_toc(&toc);

    let separator = "\n\n";
    let mut chapters: Vec<String> = Vec::new();
    let mut sections: Vec<Section> = Vec::new();
    let mut cursor = 0usize;
    for idref in &spine {
        let Some(item) = manifest.iter().find(|item| &item.id == idref) else {
            continue;
        };
        if item.is_nav {
            // The navigation document is apparatus, not prose. Ingesting it
            // adds a phantom chapter whose text is the table of contents.
            continue;
        }
        // One evaluation serves the text and the heading fallback below: a
        // second call could never fail where this one succeeded.
        let source = html_source(&decode_replace(&item.content))?;
        let text = chapter_text(&source);
        if marginalia_text::chars::strip(&text).is_empty() {
            continue;
        }
        let href = item.name.clone();
        let key = href.split('#').next().unwrap_or("").to_owned();
        let (heading, level) = match toc_map.get(&key) {
            Some((title, depth)) => (Some(title.clone()), Some(*depth)),
            None => heading_from_markup(&source),
        };
        let len = text.chars().count();
        sections.push(Section {
            char_start: cursor,
            char_end: cursor + len,
            heading,
            level,
            href: Some(href),
        });
        chapters.push(text);
        cursor += len + separator.len();
    }
    let full_text = chapters.join(separator);

    let first = |values: &[Option<String>]| values.iter().flatten().next().cloned();
    let title = first(&dc.title).unwrap_or_else(|| py_stem(file_name).to_owned());
    let author = first(&dc.author).unwrap_or_default();
    let language = first(&dc.language).unwrap_or_default();

    let mut metadata = Map::new();
    metadata.insert(
        "chapter_count".to_owned(),
        Value::from(chapters.len() as u64),
    );
    metadata.insert(
        "char_count".to_owned(),
        Value::from(full_text.chars().count() as u64),
    );
    metadata.insert("file_name".to_owned(), Value::from(file_name));
    if !author.is_empty() {
        metadata.insert("author".to_owned(), Value::from(author));
    }
    if !language.is_empty() {
        metadata.insert("language".to_owned(), Value::from(language.clone()));
    }
    for (key, values) in [
        ("publisher", &dc.publisher),
        ("date", &dc.date),
        ("description", &dc.description),
        ("identifier", &dc.identifier),
    ] {
        if let Some(value) = first(values) {
            metadata.insert(format!("dc_{key}"), Value::from(value));
        }
    }

    Ok(ParsedDocument {
        title: Some(title),
        text: full_text,
        document_type: DEFAULT_DOCUMENT_TYPE.to_owned(),
        language: if language.is_empty() {
            None
        } else {
            Some(language)
        },
        metadata,
        sections: sections.iter().map(section_map).collect(),
        structural_locators: Vec::new(),
    })
}

fn read_entry(archive: &mut zip::ZipArchive<Cursor<&[u8]>>, name: &str) -> Result<Vec<u8>> {
    let mut file = archive
        .by_name(name)
        .map_err(|_| Error::Parse(format!("cannot find {name} in archive")))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .map_err(|err| Error::Parse(format!("cannot read {name}: {err}")))?;
    Ok(bytes)
}

/// The OPF path: every matching rootfile in document order, the last one
/// standing — the loop overwrites rather than breaks.
fn find_opf(container: &Element) -> Result<String> {
    let mut opf_file: Option<String> = None;
    for rootfile in container.findall_descendants(&format!("{{{CONTAINER_NS}}}rootfile")) {
        if rootfile.attr("media-type") != Some("application/oebps-package+xml") {
            continue;
        }
        opf_file = rootfile.attr("full-path").map(str::to_owned);
    }
    opf_file.ok_or_else(|| Error::Parse("cannot find container file".to_owned()))
}

#[derive(Default)]
struct DcMetadata {
    title: Vec<Option<String>>,
    author: Vec<Option<String>>,
    language: Vec<Option<String>>,
    publisher: Vec<Option<String>>,
    date: Vec<Option<String>>,
    description: Vec<Option<String>>,
    identifier: Vec<Option<String>>,
}

/// Dublin Core text is the leading run only — `el.text`, not the whole
/// subtree — and an empty element reads as no value at all.
fn dc_text(element: &Element) -> Option<String> {
    match element.children.first() {
        Some(XmlNode::Text(text)) => Some(text.clone()),
        _ => None,
    }
}

fn read_dc_metadata(opf: &Element) -> Result<DcMetadata> {
    let mut dc = DcMetadata::default();
    let Some(metadata) = opf.find_child(&format!("{{{OPF_NS}}}metadata")) else {
        return Ok(dc);
    };
    for child in &metadata.children {
        match child {
            XmlNode::Element(element) => {
                if element.ns_uri != DC_NS {
                    continue;
                }
                let value = dc_text(element);
                match element.local.as_str() {
                    "title" => dc.title.push(value),
                    "author" => dc.author.push(value),
                    "language" => dc.language.push(value),
                    "publisher" => dc.publisher.push(value),
                    "date" => dc.date.push(value),
                    "description" => dc.description.push(value),
                    "identifier" => dc.identifier.push(value),
                    _ => {}
                }
            }
            XmlNode::Pi => {
                return Err(Error::Parse(
                    "malformed XML: processing instruction in metadata".to_owned(),
                ));
            }
            _ => {}
        }
    }
    Ok(dc)
}

struct ManifestItem {
    id: String,
    name: String,
    is_nav: bool,
    content: Vec<u8>,
}

fn read_manifest(
    opf: &Element,
    opf_dir: &str,
    archive: &mut zip::ZipArchive<Cursor<&[u8]>>,
) -> Result<Vec<ManifestItem>> {
    let mut items = Vec::new();
    let Some(manifest) = opf.find_child(&format!("{{{OPF_NS}}}manifest")) else {
        return Ok(items);
    };
    for child in manifest.child_elements() {
        if child.tag() != format!("{{{OPF_NS}}}item") {
            continue;
        }
        let media_type = child.attr("media-type").unwrap_or("").to_owned();
        // People use wrong content types; this one is corrected on the way in.
        let media_type = if media_type == "image/jpg" {
            "image/jpeg".to_owned()
        } else {
            media_type
        };
        let is_nav = media_type == "application/xhtml+xml"
            && child
                .attr("properties")
                .unwrap_or("")
                .split(' ')
                .any(|property| property == "nav");
        let raw_href = child.attr("href").unwrap_or("");
        let id = child.attr("id").unwrap_or("").to_owned();
        // The nav bytes travel the raw href; everything else the decoded
        // name. Both quirks reproduced, neither "fixed".
        let name = unquote(raw_href);
        let read_path = if is_nav {
            posix_join(opf_dir, raw_href)
        } else {
            posix_join(opf_dir, &name)
        };
        let content = read_entry(archive, &read_path)?;
        items.push(ManifestItem {
            id,
            name,
            is_nav,
            content,
        });
    }
    Ok(items)
}

/// Spine idrefs in order (linearity is read and ignored, as upstream reads
/// and ignores it) plus the spine's NCX pointer. A missing spine element
/// fails upstream too (iterating nothing is not iterable).
fn read_spine(opf: &Element) -> Result<(Vec<String>, String)> {
    let mut spine = Vec::new();
    let Some(spine_el) = opf.find_child(&format!("{{{OPF_NS}}}spine")) else {
        return Err(Error::Parse("malformed OPF: no spine".to_owned()));
    };
    let toc = spine_el.attr("toc").unwrap_or("").to_owned();
    for child in spine_el.child_elements() {
        if let Some(idref) = child.attr("idref") {
            spine.push(idref.to_owned());
        }
    }
    Ok((spine, toc))
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum TocEntry {
    Link {
        href: String,
        title: Option<String>,
    },
    Branch {
        title: Option<String>,
        href: String,
        children: Vec<TocEntry>,
    },
}

/// One NCX `navPoint`: its label (the first element child's leading text —
/// a bare or empty label reads as no label), its target, and any nested
/// points. A point with children becomes a section; without, a link.
fn ncx_node(nav_point: &Element) -> TocEntry {
    let mut label: Option<Option<String>> = None;
    let mut content = String::new();
    let mut child_points = Vec::new();
    for child in nav_point.child_elements() {
        if child.tag() == format!("{{{DAISY_NS}}}navLabel") {
            // An empty label element reads as no label, the way the
            // library's empty title does: the flattener skips both.
            label = child
                .child_elements()
                .next()
                .map(|first| match first.children.first() {
                    Some(XmlNode::Text(text)) => Some(text.clone()),
                    _ => None,
                });
        } else if child.tag() == format!("{{{DAISY_NS}}}content") {
            content = child.attr("src").unwrap_or("").to_owned();
        } else if child.tag() == format!("{{{DAISY_NS}}}navPoint") {
            child_points.push(ncx_node(child));
        }
    }
    if child_points.is_empty() {
        TocEntry::Link {
            href: content,
            title: label.unwrap_or(None),
        }
    } else {
        TocEntry::Branch {
            title: label.unwrap_or(None),
            href: content,
            children: child_points,
        }
    }
}

fn parse_ncx(raw: &[u8]) -> Result<Vec<TocEntry>> {
    let document = parse_xml(raw, TextModel::Lxml)?;
    // A missing or empty map reads as an empty table: the library yields no
    // entries for either, and chapters fall back to their own headings.
    let Some(nav_map) = document.root.find_child(&format!("{{{DAISY_NS}}}navMap")) else {
        return Ok(Vec::new());
    };
    let mut entries = Vec::new();
    for child in nav_map.child_elements() {
        if child.tag() == format!("{{{DAISY_NS}}}navPoint") {
            entries.push(ncx_node(child));
        }
    }
    Ok(entries)
}

/// One chapter's text: the body's *children*, not the body itself.
///
/// `EpubHtml.get_content` rebuilds each chapter from the source body's
/// children moved under a fresh head, so the source head (its title, its
/// scripts) never reaches the text while scripts under the body do. The
/// body's own leading text is not a moved child either, and tails are —
/// which is why a text run counts exactly when it immediately follows an
/// element. Stripped pieces join with `"\n"`, as `get_text` joins them.
fn chapter_text(html: &str) -> String {
    let document = Html::parse_document(html);
    // The parser always synthesizes a body element, even for fragments
    // and empty input (see the HTML module's proof note): selecting it
    // cannot fail.
    let body = document
        .select(&Selector::parse("body").unwrap())
        .next()
        .unwrap();
    let mut pieces: Vec<String> = Vec::new();
    let mut previous_was_element = false;
    for child in body.children() {
        match child.value() {
            scraper::Node::Text(text) => {
                // A tail rides along with its element; the body's own
                // leading text stays behind.
                if !previous_was_element {
                    continue;
                }
                previous_was_element = false;
                let stripped = marginalia_text::chars::strip(text);
                if !stripped.is_empty() {
                    pieces.push(stripped.to_owned());
                }
            }
            scraper::Node::Element(_) => {
                previous_was_element = true;
                // Matched as an element node above; wrapping it as an
                // element reference cannot fail.
                let next = scraper::ElementRef::wrap(child).expect("element node wraps");
                collect_subtree_text(next, &mut pieces);
            }
            _ => {
                previous_was_element = false;
            }
        }
    }
    pieces.join("\n")
}

fn collect_subtree_text(element: scraper::ElementRef<'_>, pieces: &mut Vec<String>) {
    for child in element.children() {
        match child.value() {
            scraper::Node::Text(text) => {
                let stripped = marginalia_text::chars::strip(text);
                if !stripped.is_empty() {
                    pieces.push(stripped.to_owned());
                }
            }
            scraper::Node::Element(_) => {
                // Matched as an element node above; wrapping it as an
                // element reference cannot fail.
                let next = scraper::ElementRef::wrap(child).expect("element node wraps");
                collect_subtree_text(next, pieces);
            }
            _ => {}
        }
    }
}

/// First heading tag and its level when the table of contents says nothing:
/// every `h1` before any `h2`, not document order. The text is
/// `get_text(strip=True)`: each string stripped on its own, empties
/// dropped, the rest welded — so a whitespace-only heading reads empty
/// and never wins. The rebuilt chapter the module searches holds the body
/// children only, so the head is not searched.
fn heading_from_markup(html: &str) -> (Option<String>, Option<u64>) {
    let document = Html::parse_document(html);
    // Bodies always synthesize; a chapter of pure markup still parses.
    let body = document
        .select(&Selector::parse("body").unwrap())
        .next()
        .unwrap();
    for level in 1..=6u64 {
        let selector = Selector::parse(&format!("h{level}")).unwrap();
        for element in document.select(&selector) {
            if !is_within(element, body) {
                continue;
            }
            let text = heading_text(element);
            if !text.is_empty() {
                return (Some(text), Some(level));
            }
        }
    }
    (None, None)
}

/// Whether `element` sits inside `ancestor`'s subtree.
fn is_within(element: scraper::ElementRef<'_>, ancestor: scraper::ElementRef<'_>) -> bool {
    let mut current = Some(element);
    while let Some(node) = current {
        if node == ancestor {
            return true;
        }
        current = node.parent().and_then(scraper::ElementRef::wrap);
    }
    false
}

/// One heading's text: stripped strings welded, empties dropped.
fn heading_text(element: scraper::ElementRef<'_>) -> String {
    let mut pieces: Vec<&str> = Vec::new();
    collect_heading_text(element, &mut pieces);
    pieces.join("")
}

fn collect_heading_text<'a>(element: scraper::ElementRef<'a>, pieces: &mut Vec<&'a str>) {
    for child in element.children() {
        match child.value() {
            scraper::Node::Text(text) => {
                let stripped = marginalia_text::chars::strip(text);
                if !stripped.is_empty() {
                    pieces.push(stripped);
                }
            }
            scraper::Node::Element(_) => {
                // Matched as an element node above; wrapping it as an
                // element reference cannot fail.
                let next = scraper::ElementRef::wrap(child).expect("element node wraps");
                collect_heading_text(next, pieces);
            }
            _ => {}
        }
    }
}

/// `text_content`: descendant text runs concatenated raw — comments and
/// PIs dropped, whitespace kept.
fn nav_text(element: scraper::ElementRef<'_>, separator: &str) -> String {
    let mut pieces: Vec<&str> = Vec::new();
    collect_nav_text(element, &mut pieces);
    pieces.join(separator)
}

fn collect_nav_text<'a>(element: scraper::ElementRef<'a>, pieces: &mut Vec<&'a str>) {
    for child in element.children() {
        match child.value() {
            scraper::Node::Text(text) => pieces.push(text),
            scraper::Node::Element(_) => {
                // Matched as an element node above; wrapping it as an
                // element reference cannot fail.
                let next = scraper::ElementRef::wrap(child).expect("element node wraps");
                collect_nav_text(next, pieces);
            }
            _ => {}
        }
    }
}

/// The nav table of contents: the first `<nav>` with any attribute equal to
/// `toc`, then its direct `<ol>`, list items in order. A nav without a toc
/// list reads as an empty table (the library finds no entries); a toc list
/// without its list fails the parse upstream too, so that failure stands.
fn parse_nav_toc(document: &Html, base: &str) -> Result<Vec<TocEntry>> {
    let Some(nav) = document
        .select(&Selector::parse("nav").unwrap())
        .find(|el| el.value().attrs().any(|(_, value)| value == "toc"))
    else {
        return Ok(Vec::new());
    };
    let ol = nav
        .children()
        .filter_map(scraper::ElementRef::wrap)
        .find(|el| el.value().name() == "ol")
        .ok_or_else(|| Error::Parse("nav table of contents has no list".to_owned()))?;
    Ok(parse_nav_list(ol, base))
}

fn parse_nav_list(ol: scraper::ElementRef<'_>, base: &str) -> Vec<TocEntry> {
    ol.children()
        .filter_map(scraper::ElementRef::wrap)
        .filter(|el| el.value().name() == "li")
        .flat_map(|li| parse_nav_item(li, base))
        .collect()
}

/// One nav list item: a section for a sublist, a link for an address, and
/// nothing at all for anything else.
fn parse_nav_item(li: scraper::ElementRef<'_>, base: &str) -> Vec<TocEntry> {
    let sublist = li
        .children()
        .filter_map(scraper::ElementRef::wrap)
        .find(|el| el.value().name() == "ol");
    let link = li
        .children()
        .filter_map(scraper::ElementRef::wrap)
        .find(|el| el.value().name() == "a");
    if let Some(sub) = sublist {
        // The title is the first child's text whatever it is; a link
        // without an href still opens a section, just an unaddressed one.
        let first = li
            .children()
            .filter_map(scraper::ElementRef::wrap)
            .next()
            .map(|el| nav_text(el, ""))
            .unwrap_or_default();
        let children = parse_nav_list(sub, base);
        let href = link
            .and_then(|el| el.value().attr("href"))
            .filter(|href| !href.is_empty())
            .map(|href| posix_normpath(&posix_join(base, href)))
            .unwrap_or_default();
        return vec![TocEntry::Branch {
            title: Some(first),
            href,
            children,
        }];
    }
    if let Some(link) = link {
        if let Some(href) = link.value().attr("href") {
            if !href.is_empty() {
                return vec![TocEntry::Link {
                    href: posix_normpath(&posix_join(base, href)),
                    title: Some(nav_text(link, "")),
                }];
            }
        }
    }
    Vec::new()
}

/// The page list, parsed for its errors and otherwise dropped: nothing this
/// module returns reads it.
fn parse_nav_pages(document: &Html, base: &str) -> Result<()> {
    let Some(nav) = document
        .select(&Selector::parse("nav").unwrap())
        .find(|el| el.value().attrs().any(|(_, value)| value == "page-list"))
    else {
        return Ok(());
    };
    let Some(ol) = nav
        .children()
        .filter_map(scraper::ElementRef::wrap)
        .find(|el| el.value().name() == "ol")
    else {
        return Err(Error::Parse("nav page list has no list".to_owned()));
    };
    let _ = parse_nav_list(ol, base);
    Ok(())
}

/// Map each table target to its `(title, depth)`: fragments discarded (the
/// spine addresses whole files), the shallowest entry winning.
fn flatten_toc(entries: &[TocEntry]) -> HashMap<String, (String, u64)> {
    let mut flat = HashMap::new();
    flatten_into(entries, 1, &mut flat);
    flat
}

fn flatten_into(entries: &[TocEntry], depth: u64, flat: &mut HashMap<String, (String, u64)>) {
    for entry in entries {
        match entry {
            TocEntry::Link { href, title } => {
                if let Some(title) = title {
                    if !href.is_empty() && !title.is_empty() {
                        let key = href.split('#').next().unwrap_or("");
                        flat.entry(key.to_owned()).or_insert((title.clone(), depth));
                    }
                }
            }
            TocEntry::Branch {
                title,
                href,
                children,
            } => {
                if let Some(title) = title {
                    if !href.is_empty() && !title.is_empty() {
                        let key = href.split('#').next().unwrap_or("");
                        flat.entry(key.to_owned()).or_insert((title.clone(), depth));
                    }
                }
                flatten_into(children, depth + 1, flat);
            }
        }
    }
}

/// `posixpath.join`, absolute second sides winning.
pub fn posix_join(base: &str, path: &str) -> String {
    if path.starts_with('/') || base.is_empty() {
        path.to_owned()
    } else {
        format!("{base}/{path}")
    }
}

/// `posixpath.dirname`, root included.
pub fn posix_dirname(path: &str) -> &str {
    match path.rfind('/') {
        None => "",
        Some(0) => "/",
        Some(i) => &path[..i],
    }
}

/// `posixpath.normpath`, leading double slashes preserved the way POSIX
/// leaves them implementation-defined.
pub fn posix_normpath(path: &str) -> String {
    let absolute = path.starts_with('/');
    let double_slash = path.starts_with("//") && !path.starts_with("///");
    let mut parts: Vec<&str> = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if parts.last().is_some_and(|last| *last != "..") {
                    parts.pop();
                } else if !absolute {
                    parts.push("..");
                }
            }
            _ => parts.push(part),
        }
    }
    let mut out = parts.join("/");
    if absolute {
        out.insert(0, '/');
        if double_slash {
            out.insert(0, '/');
        }
    }
    if out.is_empty() {
        out.push('.');
    }
    out
}

/// `urllib.parse.unquote`: percent-decoding over bytes re-read as UTF-8
/// with replacement, a lone `%` left literal, `+` untouched.
pub fn unquote(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        let pair = bytes.get(i + 1..i + 3);
        if bytes[i] == b'%' && pair.is_some_and(|pair| pair.iter().all(|b| b.is_ascii_hexdigit())) {
            let digits = pair.unwrap_or(&[]);
            let hi = (digits[0] as char).to_digit(16).unwrap_or(0);
            let lo = (digits[1] as char).to_digit(16).unwrap_or(0);
            out.push((hi * 16 + lo) as u8);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}
