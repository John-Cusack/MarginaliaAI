//! A minimal ElementTree-model DOM over `quick-xml`, shared by the TEI and
//! EPUB parsers (and the `wlc_extract` binary).
//!
//! Python parses with lxml (`etree.parse`) or the stdlib
//! (`xml.etree.ElementTree`), whose models differ in exactly one place that
//! matters here: a comment or processing instruction *splits* lxml text
//! (its inner text is dropped, the runs around it stay separate) but is
//! invisible to `ElementTree` (the runs join seamlessly). [`TextModel`]
//! selects which one a parse builds. Both drop the comment/PI content
//! itself — verified against both interpreters — and both resolve CDATA as
//! text, predefined and character references, and internal-subset general
//! entities.
//!
//! What stays an error, mirroring both parsers: undefined entities and
//! prefixes, malformed markup, extra top-level content, non-whitespace
//! outside the root, and any external DTD subset (both sides refuse to
//! fetch it — expat loudly and libxml2 by configuration).

use std::collections::HashMap;
use std::sync::LazyLock;

use marginalia_types::{Error, Result};
use quick_xml::escape::{resolve_xml_entity, unescape_with};
use quick_xml::events::Event;
use quick_xml::Reader;
use regex::Regex;

/// Which interpreter's text model a parse builds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TextModel {
    /// lxml: comments/PIs split text runs; their content is dropped.
    Lxml,
    /// `xml.etree`: comments/PIs vanish; the runs around them merge.
    Et,
}

/// One element: namespace URI plus local name, attributes by local name,
/// and children where text runs and tails are plain [`XmlNode::Text`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Element {
    /// Namespace URI, or `""` when the tag carries none.
    pub ns_uri: String,
    /// The tag without its namespace.
    pub local: String,
    /// The qualified name as written (`prefix:local` or `local`), kept so
    /// end tags compare the way the parsers compare them.
    pub qname: String,
    /// `(local name, value)` attributes; `xmlns` declarations excluded.
    pub attrs: Vec<(String, String)>,
    /// Child elements, text runs (including tails), comments, PIs.
    pub children: Vec<XmlNode>,
}

/// A child node. Comments and PIs keep no content: both interpreters drop
/// it, so keeping it could only leak into text it never reaches.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum XmlNode {
    Element(Box<Element>),
    Text(String),
    Comment,
    Pi,
}

impl Element {
    /// Clark notation (`{uri}local`, bare `local` without a namespace),
    /// exactly what both interpreters compare tags in.
    pub fn tag(&self) -> String {
        if self.ns_uri.is_empty() {
            self.local.clone()
        } else {
            format!("{{{}}}{}", self.ns_uri, self.local)
        }
    }

    /// An attribute by local name.
    pub fn attr(&self, name: &str) -> Option<&str> {
        self.attrs
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }

    /// `el.text`: the leading text run, or `""`.
    pub fn text(&self) -> &str {
        match self.children.first() {
            Some(XmlNode::Text(text)) => text,
            _ => "",
        }
    }

    /// `"".join(el.itertext())`: every text run under the element.
    pub fn itertext(&self) -> String {
        let mut out = String::new();
        self.collect_text(&mut out);
        out
    }

    fn collect_text(&self, out: &mut String) {
        for child in &self.children {
            match child {
                XmlNode::Text(text) => out.push_str(text),
                XmlNode::Element(element) => element.collect_text(out),
                XmlNode::Comment | XmlNode::Pi => {}
            }
        }
    }

    /// Direct child elements.
    pub fn child_elements(&self) -> impl Iterator<Item = &Element> {
        self.children.iter().filter_map(|child| match child {
            XmlNode::Element(element) => Some(element.as_ref()),
            _ => None,
        })
    }

    /// All descendant elements, in document order.
    pub fn descendants(&self) -> Vec<&Element> {
        let mut out = Vec::new();
        self.collect_descendants(&mut out);
        out
    }

    fn collect_descendants<'a>(&'a self, out: &mut Vec<&'a Element>) {
        for child in self.child_elements() {
            out.push(child);
            child.collect_descendants(out);
        }
    }

    /// First direct child with this Clark tag.
    pub fn find_child(&self, tag: &str) -> Option<&Element> {
        self.child_elements().find(|el| el.tag() == tag)
    }

    /// All descendants with this Clark tag, in document order.
    pub fn findall_descendants(&self, tag: &str) -> Vec<&Element> {
        self.descendants()
            .into_iter()
            .filter(|el| el.tag() == tag)
            .collect()
    }

    /// The tail after the `index`-th child: the text run following an
    /// element child, or `""`.
    pub fn tail_after(&self, index: usize) -> &str {
        match (self.children.get(index), self.children.get(index + 1)) {
            (Some(XmlNode::Element(_)), Some(XmlNode::Text(text))) => text,
            _ => "",
        }
    }
}

/// A parsed document: its root element.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct XmlDocument {
    pub root: Element,
}

/// Qualify a local name the way the `tei:` prefix does: Clark notation
/// under the namespace, or the bare name without one.
fn qualify(local: &str, ns_uri: &str) -> String {
    if ns_uri.is_empty() {
        local.to_owned()
    } else {
        format!("{{{ns_uri}}}{local}")
    }
}

impl Element {
    /// First match of [`Element::findall`]'s paths, from this element.
    pub fn find(&self, path: &str, ns_uri: &str) -> Option<&Element> {
        self.findall(path, ns_uri).into_iter().next()
    }

    /// Tiny path engine for exactly the shapes the TEI module uses: `name`
    /// (direct children) and `.//a/b/c` (descendants `a`, then children).
    /// Segments are local names; `ns_uri` plays the `tei:` prefix's role.
    pub fn findall(&self, path: &str, ns_uri: &str) -> Vec<&Element> {
        if let Some(rest) = path.strip_prefix(".//") {
            let mut parts = rest.split('/');
            let first = parts.next().unwrap_or("");
            let mut current = self.findall_descendants(&qualify(first, ns_uri));
            for part in parts {
                let tag = qualify(part, ns_uri);
                current = current
                    .into_iter()
                    .flat_map(|el| {
                        el.child_elements()
                            .filter(|child| child.tag() == tag)
                            .collect::<Vec<_>>()
                    })
                    .collect();
            }
            current
        } else {
            let tag = qualify(path, ns_uri);
            self.child_elements().filter(|el| el.tag() == tag).collect()
        }
    }

    /// Every text run under the element, in document order — the segments
    /// `itertext()` yields, before any stripping.
    pub fn text_segments(&self) -> Vec<&str> {
        let mut out = Vec::new();
        self.collect_segments(&mut out);
        out
    }

    fn collect_segments<'a>(&'a self, out: &mut Vec<&'a str>) {
        for child in &self.children {
            match child {
                XmlNode::Text(text) => out.push(text),
                XmlNode::Element(element) => element.collect_segments(out),
                XmlNode::Comment | XmlNode::Pi => {}
            }
        }
    }
}

impl XmlDocument {
    /// First match of [`Element::findall`]'s paths, from the root.
    pub fn find(&self, path: &str, ns_uri: &str) -> Option<&Element> {
        self.root.find(path, ns_uri)
    }

    /// All matches, in document order, from the root.
    pub fn findall(&self, path: &str, ns_uri: &str) -> Vec<&Element> {
        self.root.findall(path, ns_uri)
    }
}

static ENTITY_DEF: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"<!ENTITY\s+(\S+)\s+(?:"([^"]*)"|'([^']*)')"#).unwrap());

/// One scope of prefix bindings; `""` is the default namespace. Scopes nest
/// with elements, and an element's own declarations bind for itself.
#[derive(Default)]
struct NsStack {
    scopes: Vec<HashMap<String, String>>,
}

impl NsStack {
    fn lookup(&self, prefix: Option<&str>) -> Option<String> {
        let key = prefix.unwrap_or("");
        if key == "xml" {
            return Some("http://www.w3.org/XML/1998/namespace".to_owned());
        }
        self.scopes
            .iter()
            .rev()
            .find_map(|scope| scope.get(key).cloned())
    }
}

/// The failure reading text off a comment or PI raises: lxml's `itertext`
/// refuses non-elements, so text extraction that reaches one fails.
pub fn non_element_text_error() -> marginalia_types::Error {
    marginalia_types::Error::Parse(
        "malformed XML: text of a comment or processing instruction".to_owned(),
    )
}

/// Borrowed bytes that came out of the input `&str`: valid UTF-8 by
/// construction, so this lossy conversion is exact and infallible — there
/// is no error arm to cover because none can fire.
fn exact_str(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

/// Parse bytes the way `etree.parse` does: the XML declaration's encoding
/// wins, plain documents are strict UTF-8, and anything undecodable fails.
pub fn parse_xml(raw: &[u8], model: TextModel) -> Result<XmlDocument> {
    let bytes = raw.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(raw);
    let text = decode_xml_bytes(bytes)?;
    build_document(&text, model)
}

fn decode_xml_bytes(bytes: &[u8]) -> Result<std::borrow::Cow<'_, str>> {
    match encoding_label(bytes) {
        None => std::str::from_utf8(bytes)
            .map(std::borrow::Cow::Borrowed)
            .map_err(|err| Error::Parse(format!("not valid UTF-8: {err}"))),
        Some(label) => decode_declared_bytes(&label, bytes),
    }
}

/// Decode under an XML-declared encoding the way the interpreters do:
/// WHATWG label resolution (which remaps `ascii` and `latin-1` onto
/// windows-1252) does not apply here — Python resolves through its own
/// codecs, where ASCII is strict and latin-1 is the bytewise map.
fn decode_declared_bytes<'a>(
    label: &str,
    bytes: &'a [u8],
) -> Result<std::borrow::Cow<'a, str>, Error> {
    let folded = label.trim().to_lowercase().replace('_', "-");
    // Strict ASCII under any of its codec aliases.
    if matches!(
        folded.as_str(),
        "ascii" | "us-ascii" | "646" | "ansi-x3.4-1968" | "ansi-x3.4-1986"
    ) {
        if bytes.iter().any(|b| *b >= 0x80) {
            return Err(Error::Parse(
                "not valid ascii: undecodable bytes".to_owned(),
            ));
        }
        // The scan above proved every byte ASCII, hence valid UTF-8:
        // this conversion cannot fail.
        return Ok(std::str::from_utf8(bytes)
            .map(std::borrow::Cow::Borrowed)
            .expect("ASCII bytes are valid UTF-8"));
    }
    // Windows-1252 fails its five undefined bytes upstream, so they fail
    // here too (the decoder itself stays silent about them).
    if matches!(folded.as_str(), "windows-1252" | "cp1252")
        && bytes
            .iter()
            .any(|b| matches!(b, 0x81 | 0x8D | 0x8F | 0x90 | 0x9D))
    {
        return Err(Error::Parse(
            "not valid windows-1252: undecodable bytes".to_owned(),
        ));
    }
    // Bytewise latin-1, infallible on both sides.
    if matches!(
        folded.as_str(),
        "latin-1" | "latin1" | "iso-8859-1" | "iso8859-1" | "8859" | "l1"
    ) {
        return Ok(bytes.iter().map(|b| *b as char).collect::<String>().into());
    }
    let encoding = encoding_rs::Encoding::for_label(label.as_bytes())
        .ok_or_else(|| Error::Parse(format!("unknown XML encoding: {label}")))?;
    let (text, _, had_errors) = encoding.decode(bytes);
    if had_errors {
        return Err(Error::Parse(format!(
            "not valid {label}: undecodable bytes"
        )));
    }
    Ok(text)
}

/// The `encoding` pseudo-attribute of a leading `<?xml ...?>` declaration,
/// scanned byte-wise so a non-UTF-8 prologue still reads.
fn encoding_label(bytes: &[u8]) -> Option<String> {
    if !bytes.starts_with(b"<?xml") {
        return None;
    }
    let rest = &bytes[5..];
    if rest.first().is_none_or(|b| !b.is_ascii_whitespace()) {
        return None;
    }
    let end = rest.windows(2).position(|w| w == b"?>")?;
    let decl = std::str::from_utf8(&rest[..end]).ok()?;
    let caps = Regex::new(r#"encoding\s*=\s*(?:"([^"]+)"|'([^']+)')"#)
        .unwrap()
        .captures(decl)?;
    caps.get(1)
        .or_else(|| caps.get(2))
        .map(|m| m.as_str().to_owned())
}

fn build_document(text: &str, model: TextModel) -> Result<XmlDocument> {
    // XML line-end normalization happens below parsing: CR and CRLF are LF
    // before anything else reads the document, on both interpreters.
    let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
    let mut reader = Reader::from_str(&normalized);
    let mut buf = Vec::new();
    let mut stack: Vec<Element> = Vec::new();
    let mut namespaces = NsStack::default();
    let mut root: Option<Element> = None;
    let mut entities: HashMap<String, String> = HashMap::new();
    let mut decl_seen = false;
    let mut doctype_seen = false;
    // Anything before the declaration — even whitespace or a comment —
    // forbids it, exactly as libxml2 forbids it.
    let mut prolog_content = false;

    loop {
        match reader.read_event_into(&mut buf) {
            Err(err) => return Err(Error::Parse(format!("malformed XML: {err}"))),
            Ok(Event::Eof) => break,
            Ok(Event::Decl(_)) => {
                // A declaration anywhere past the prologue is as fatal here
                // as it is to libxml2.
                if decl_seen || prolog_content || !stack.is_empty() || root.is_some() {
                    return Err(Error::Parse(
                        "malformed XML: declaration not at start".to_owned(),
                    ));
                }
                decl_seen = true;
            }
            Ok(Event::DocType(doctype)) => {
                if doctype_seen || !stack.is_empty() || root.is_some() {
                    return Err(Error::Parse(
                        "malformed XML: doctype out of place".to_owned(),
                    ));
                }
                doctype_seen = true;
                prolog_content = true;
                let content = exact_str(&doctype);
                // External subsets are never fetched: without validation
                // the parsers do not read them, and references to
                // externally-defined entities fail below as undefined.
                collect_entities(&content, &mut entities);
            }
            Ok(Event::Start(start)) => {
                prolog_content = true;
                if root.is_some() && stack.is_empty() {
                    return Err(Error::Parse(
                        "malformed XML: extra content past root".to_owned(),
                    ));
                }
                let (element, declarations) = open_element(&start, &namespaces, &entities)?;
                namespaces.scopes.push(declarations);
                stack.push(element);
            }
            Ok(Event::Empty(empty)) => {
                prolog_content = true;
                if root.is_some() && stack.is_empty() {
                    return Err(Error::Parse(
                        "malformed XML: extra content past root".to_owned(),
                    ));
                }
                let (element, _) = open_element(&empty, &namespaces, &entities)?;
                attach(&mut stack, &mut root, element);
            }
            Ok(Event::End(_)) => {
                // quick-xml refuses unmatched end tags itself, so the stack
                // is never empty here; the yield is the proof.
                let element = stack.pop().expect("End events match an open element");
                namespaces.scopes.pop();
                // quick-xml checks end-tag names itself before yielding, so
                // a mismatch never reaches here: it fails there, the way
                // both interpreters fail on it.
                attach(&mut stack, &mut root, element);
            }
            Ok(Event::Text(text)) => {
                prolog_content = true;
                let decoded = exact_str(&text);
                // The tokenizer surfaces every `&`-led reference as its own
                // event (or fails the read first), so text runs never hold
                // one and this expansion cannot fail: quick-xml 0.39 splits
                // references out of text (probed), and the expander only
                // fails on references. Re-verify if quick-xml upgrades.
                let resolved =
                    resolve_entities(&decoded, &entities).expect("text events hold no references");
                match push_text(&mut stack, &mut root, &resolved) {
                    Ok(()) => (),
                    Err(err) => return Err(err),
                }
            }
            Ok(Event::CData(cdata)) => {
                // Character data is raw by definition: no entity pass.
                let raw = exact_str(&cdata);
                match push_text(&mut stack, &mut root, &raw) {
                    Ok(()) => (),
                    Err(err) => return Err(err),
                }
            }
            Ok(Event::GeneralRef(entity)) => {
                prolog_content = true;
                // An entity reference where the tokenizer surfaces one
                // rather than folding it into text: expand and treat as
                // text, which is what the reference contributes.
                let name: &[u8] = &entity;
                let name = exact_str(name);
                // Spelled out because `?` leaves no countable region here
                // (verified during development: the error tests execute this
                // path while the `?` region reads zero).
                #[allow(clippy::question_mark)]
                let resolved = match resolve_entities(&format!("&{name};"), &entities) {
                    Ok(resolved) => resolved,
                    Err(err) => return Err(err),
                };
                match push_text(&mut stack, &mut root, &resolved) {
                    Ok(()) => (),
                    Err(err) => return Err(err),
                }
            }
            Ok(Event::Comment(_)) => {
                prolog_content = true;
                if model == TextModel::Lxml && !stack.is_empty() {
                    push_inside(&mut stack, XmlNode::Comment);
                }
            }
            Ok(Event::PI(_)) => {
                prolog_content = true;
                if model == TextModel::Lxml && !stack.is_empty() {
                    push_inside(&mut stack, XmlNode::Pi);
                }
            }
        }
        buf.clear();
    }

    if !stack.is_empty() {
        return Err(Error::Parse("malformed XML: unclosed elements".to_owned()));
    }
    root.map(|root| XmlDocument { root })
        .ok_or_else(|| Error::Parse("malformed XML: empty document".to_owned()))
}

fn attach(stack: &mut [Element], root: &mut Option<Element>, element: Element) {
    match stack.last_mut() {
        Some(parent) => {
            parent.children.push(XmlNode::Element(Box::new(element)));
        }
        None => {
            // Unreachable past the start-tag/empty-tag guards, which refuse
            // top-level content once the root exists: the stack is empty
            // here only before any root is set.
            *root = Some(element);
        }
    }
}

fn push_inside(stack: &mut [Element], node: XmlNode) {
    // Top-level comments and PIs drop: with no parent the map has nothing
    // to hold them in.
    let _ = stack.last_mut().map(|parent| parent.children.push(node));
}

/// Text at the top level must be whitespace; inside an element it merges
/// with a preceding run, which is what keeps `ElementTree` text contiguous
/// once comments vanish.
fn push_text(stack: &mut [Element], root: &mut Option<Element>, text: &str) -> Result<()> {
    match stack.last_mut() {
        Some(parent) => {
            if let Some(XmlNode::Text(prev)) = parent.children.last_mut() {
                prev.push_str(text);
            } else if !text.is_empty() {
                parent.children.push(XmlNode::Text(text.to_owned()));
            }
            Ok(())
        }
        None => {
            if text.chars().all(|c| c.is_whitespace()) {
                return Ok(());
            }
            let _ = root;
            Err(Error::Parse(
                "malformed XML: content outside root".to_owned(),
            ))
        }
    }
}

/// Build one element against the current scopes, returning the element and
/// its own declarations (which bind for the element itself and below).
fn open_element(
    element: &quick_xml::events::BytesStart<'_>,
    namespaces: &NsStack,
    entities: &HashMap<String, String>,
) -> Result<(Element, HashMap<String, String>)> {
    let raw = String::from_utf8_lossy(element.name().as_ref()).into_owned();
    let (prefix, local) = match raw.rsplit_once(':') {
        Some((prefix, local)) => (Some(prefix.to_owned()), local.to_owned()),
        None => (None, raw.clone()),
    };

    let mut declarations: HashMap<String, String> = HashMap::new();
    let mut attrs: Vec<(String, String)> = Vec::new();
    for attr in element.attributes() {
        let attr =
            attr.map_err(|err| Error::Parse(format!("malformed XML: bad attribute: {err}")))?;
        let key = String::from_utf8_lossy(attr.key.as_ref()).into_owned();
        // Attribute values arrive decoded already; only the XML-mandated
        // line-space folding plus entity expansion remain. CDATA-type
        // attributes (everything read here) fold literal tab/CR/LF to a
        // space while references to those characters survive.
        let raw_value = exact_str(attr.value.as_ref());
        let folded = raw_value.replace(['\t', '\r', '\n'], " ");
        let value = unescape_with(&folded, |name| {
            resolve_xml_entity(name).or_else(|| entities.get(name).map(String::as_str))
        })
        .map_err(|err| Error::Parse(format!("malformed XML: bad attribute value: {err}")))?
        .into_owned();
        if key == "xmlns" {
            declarations.insert(String::new(), value);
        } else if let Some(name) = key.strip_prefix("xmlns:") {
            declarations.insert(name.to_owned(), value);
        } else {
            attrs.push((key, value));
        }
    }

    // The element's own declarations bind for itself.
    let mut effective = namespaces.scopes.clone();
    effective.push(declarations.clone());
    let scoped = NsStack { scopes: effective };
    let ns_uri = match prefix.as_deref() {
        None => scoped.lookup(None).unwrap_or_default(),
        Some("xmlns") => {
            return Err(Error::Parse(
                "malformed XML: xmlns used as element prefix".to_owned(),
            ));
        }
        Some(prefix) => scoped
            .lookup(Some(prefix))
            .ok_or_else(|| Error::Parse(format!("malformed XML: undefined prefix: {prefix}")))?,
    };

    let mut resolved_attrs = Vec::with_capacity(attrs.len());
    for (key, value) in attrs {
        match key.rsplit_once(':') {
            None => resolved_attrs.push((key, value)),
            Some((attr_prefix, attr_local)) => {
                if attr_prefix != "xml" && scoped.lookup(Some(attr_prefix)).is_none() {
                    return Err(Error::Parse(format!(
                        "malformed XML: undefined prefix: {attr_prefix}"
                    )));
                }
                resolved_attrs.push((attr_local.to_owned(), value));
            }
        }
    }

    Ok((
        Element {
            ns_uri,
            local,
            qname: raw,
            attrs: resolved_attrs,
            children: Vec::new(),
        },
        declarations,
    ))
}

/// Internal-subset general entities (`<!ENTITY name "value">`), the only
/// declarations either interpreter resolves in memory. Parameter entities
/// (`%`) declare markup, never text, and are skipped like both sides skip
/// them.
fn collect_entities(content: &str, entities: &mut HashMap<String, String>) {
    for caps in ENTITY_DEF.captures_iter(content) {
        let name = caps.get(1).map(|m| m.as_str()).unwrap_or("");
        // Parameter entities (`%`) declare markup, never text.
        if name.starts_with('%') {
            continue;
        }
        let value = caps
            .get(2)
            .or_else(|| caps.get(3))
            .map(|m| m.as_str())
            .unwrap_or("");
        entities
            .entry(name.to_owned())
            .or_insert_with(|| value.to_owned());
    }
}

/// Fully expand a text run. `unescape_with` is one pass, so nested entity
/// values loop until they settle. A pass that changes nothing while an
/// ampersand remains is a cycle, and a runaway (hostile nesting) stops at
/// the length guard — both fail the way expat fails them.
fn resolve_entities(text: &str, entities: &HashMap<String, String>) -> Result<String> {
    if !text.contains('&') {
        return Ok(text.to_owned());
    }
    let mut current = text.to_owned();
    // A few thousand passes settle any acyclic nesting a document can
    // hold; a cycle oscillates forever and a hostile one trips the length
    // guard first — both fail the way expat fails them.
    for _ in 0..1024 {
        let next: String = unescape_with(&current, |name| {
            resolve_xml_entity(name).or_else(|| entities.get(name).map(String::as_str))
        })
        .map_err(|err| Error::Parse(format!("malformed XML: bad entity: {err}")))?
        .into_owned();
        if next == current {
            return Err(Error::Parse(
                "malformed XML: entity expansion does not terminate".to_owned(),
            ));
        }
        if next.len() > 10_000_000 {
            return Err(Error::Parse(
                "malformed XML: entity expansion does not terminate".to_owned(),
            ));
        }
        current = next;
        if !current.contains('&') {
            return Ok(current);
        }
    }
    Err(Error::Parse(
        "malformed XML: entity expansion does not terminate".to_owned(),
    ))
}
