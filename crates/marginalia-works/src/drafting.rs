//! Render a draft to markdown and read an edited one back.
//!
//! Python source: `services/works/drafting.py`. `export_draft` regenerates
//! the §6.4 file from rows; `import_draft` parses edited markdown per the
//! §5.2 block boundaries, copies the revision forward, and applies inserts,
//! updates, deletions, and reorders, matching blocks by their comments.
//!
//! [`render_markdown`] and [`parse_markdown`] are pure and ported 1:1. The
//! DB orchestration ([`WorkExportService`]) stays generic over the port
//! traits so the rules compile and run against in-memory doubles; real
//! adapters land with Phase 5.
//!
//! YAML front matter is emitted by the [`YamlEmitter`] below, a faithful
//! port of PyYAML's `SafeDumper` block path (`Emitter` plus the
//! `SafeRepresenter`/`Resolver` scalar rules for the shapes the exporter
//! builds): scalar analysis, implicit-resolution quoting, indentless
//! sequences, and width-80 continuation wrapping. `serde_yaml` only reads
//! front matter back; its emitter is never on the export path.

use std::collections::HashMap;
use std::fmt;
use std::sync::LazyLock;

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use marginalia_types::ports::{
    CitationRepo, SourceSpanRepo, WorkBlockRepo, WorkLinkRepo, WorkRepo, WorkRevisionRepo,
};
use marginalia_types::works::{Placement, RevisionState, Work, WorkBlockDraft, WorkRevision};
use marginalia_types::works_files::Intent;
use marginalia_types::works_ports::TxFactory;
use marginalia_types::{Error, Result};

use crate::assembly::{assemble_revision, AssembledRevision};
use crate::markers::{find_markers, format_marker};
use marginalia_text::repr::py_repr_str;

/// Python `\s` for `str` patterns: Unicode whitespace plus U+001C-U+001F
/// (which `str.strip`/`isspace` honor but `\p{White_Space}` omits). Every
/// `\s` below is spelled out so the scanner classes match CPython's.
pub static BLOCK_COMMENT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"^<!--[\p{White_Space}\p{Z}\x1c-\x1f]*block:([0-9a-fA-F-]{36})[\p{White_Space}\p{Z}\x1c-\x1f]*-->$",
    )
    .expect("block comment regex")
});
pub static TITLE_COMMENT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"^<!--[\p{White_Space}\p{Z}\x1c-\x1f]*title:[\p{White_Space}\p{Z}\x1c-\x1f]*(.*?)[\p{White_Space}\p{Z}\x1c-\x1f]*-->$",
    )
    .expect("title comment regex")
});
pub static HEADING_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(#{1,6})[\p{White_Space}\p{Z}\x1c-\x1f]+(.*)$").expect("heading regex")
});
pub static FOOTNOTE_DEF_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\[\^[^\]]+\]:.*$").expect("footnote def regex"));
pub static LIST_ITEM_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[\p{White_Space}\p{Z}\x1c-\x1f]*([-*+]|\d+[.)])[\p{White_Space}\p{Z}\x1c-\x1f]+")
        .expect("list item regex")
});
pub static FRONT_MATTER_RE: LazyLock<Regex> = LazyLock::new(|| {
    // `\z` is the regex crate's spelling of Python's `\Z` (absolute end).
    Regex::new(r"(?s)\A---\n(.*?)\n---(?:\n|\z)").expect("front matter regex")
});

/// The import was refused with nothing written; the tool reports the rule.
#[derive(Debug, Clone, PartialEq)]
pub struct ImportRefused {
    pub rule_id: String,
    pub message: String,
    pub detail: Option<Map<String, Value>>,
}

impl fmt::Display for ImportRefused {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ImportRefused {}

/// Import failure: a rule refusal, or an infrastructure/validation error
/// (mirroring `ValueError` and `NotFoundError` from the Python).
#[derive(Debug)]
pub enum ImportError {
    Refused(ImportRefused),
    Failed(Error),
}

impl fmt::Display for ImportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Refused(refused) => write!(formatter, "{refused}"),
            Self::Failed(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for ImportError {}

impl From<Error> for ImportError {
    fn from(error: Error) -> Self {
        Self::Failed(error)
    }
}

pub type ImportResult<T> = std::result::Result<T, ImportError>;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BlockChange {
    pub block_key: String,
    pub change: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ImportDiff {
    pub revision_id: Uuid,
    pub revision_number: i64,
    pub dry_run: bool,
    #[serde(default)]
    pub changes: Vec<BlockChange>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ParsedBlock {
    pub key: Option<Uuid>,
    pub block_type: String,
    pub title: Option<String>,
    pub body: String,
    pub level: i64,
    /// Index of the parent in the parsed list, for heading nesting.
    pub parent_index: Option<usize>,
}
// --- Python `str` whitespace helpers --------------------------------------
//
// CPython's `strip`/`isspace` set is `\p{White_Space}` plus U+001C-U+001F.
// Rust's `char::is_whitespace` covers only the former, so every strip,
// emptiness check, and first-character test in the scanner goes through
// these.

/// One Python-`str` whitespace character.
fn py_is_space(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{1f}')
}

/// `line.strip()`: both ends.
fn py_strip(text: &str) -> &str {
    let start = text.find(|ch: char| !py_is_space(ch)).unwrap_or(text.len());
    let end = text
        .char_indices()
        .filter(|(_, ch)| !py_is_space(*ch))
        .map(|(index, ch)| index + ch.len_utf8())
        .next_back()
        .unwrap_or(0);
    if start >= end {
        ""
    } else {
        &text[start..end]
    }
}

/// `str.splitlines()`: Python splits on `\n`, `\r\n`, `\r`, `\v`, `\f`,
/// U+001C-U+001E, U+0085, U+2028, and U+2029. Rust's `str::lines` splits
/// only on `\n`/`\r\n`, so the boundary set is spelled out here; every
/// boundary is consumed with no trailing empty item, exactly like
/// `splitlines` (in particular `"a\n".splitlines() == ["a"]`).
fn py_splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut index = 0;
    while index < text.len() {
        let ch = text[index..].chars().next().expect("char boundary");
        let boundary = match ch {
            '\n' => 1,
            '\r' if text[index + 1..].starts_with('\n') => 2,
            '\r' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{1f}' | '' | ' '
            | ' ' => ch.len_utf8(),
            _ => 0,
        };
        if boundary > 0 {
            lines.push(&text[start..index]);
            index += boundary;
            start = index;
        } else {
            index += ch.len_utf8();
        }
    }
    if start < text.len() || text.is_empty() && lines.is_empty() {
        lines.push(&text[start..]);
    }
    lines
}

// --- YAML emission (PyYAML `safe_dump` faithful port) ----------------------
//
// The exporter builds only ordered mappings of scalars, sequences of
// mappings, and nested plain JSON values. The emitter below reproduces
// PyYAML's block path for exactly those shapes: `best_indent=2`,
// `best_width=80`, `allow_unicode=True`, no canonical form, no anchors,
// no tags, no document markers.

/// A front-matter value in exporter order.
#[derive(Debug, Clone, PartialEq)]
enum YamlNode {
    Str(String),
    Int(i64),
    Bool(bool),
    Null,
    Float(f64),
    Seq(Vec<YamlNode>),
    /// Ordered mapping pairs; insertion order is the emission order.
    Map(Vec<(String, YamlNode)>),
}

/// The scalar tag the representer assigned before resolution.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum YamlTag {
    Str,
    Bool,
    Int,
    Float,
    Null,
    Merge,
    Timestamp,
    Value,
}

fn json_to_node(value: &Value) -> YamlNode {
    match value {
        Value::Null => YamlNode::Null,
        Value::Bool(flag) => YamlNode::Bool(*flag),
        Value::Number(number) => {
            if let Some(int) = number.as_i64() {
                YamlNode::Int(int)
            } else if let Some(uint) = number.as_u64() {
                YamlNode::Int(uint.min(i64::MAX as u64) as i64)
            } else {
                // Proof the old `Str(number.to_string())` fallback is gone:
                // without `arbitrary_precision` (Cargo.toml enables only
                // `preserve_order`) every `Number` is u64/i64/f64, so after
                // the int arms only the f64 case remains and `expect`
                // documents the infallible tail.
                YamlNode::Float(
                    number
                        .as_f64()
                        .expect("serde_json Number is always u64/i64/f64"),
                )
            }
        }
        Value::String(text) => YamlNode::Str(text.clone()),
        Value::Array(items) => YamlNode::Seq(items.iter().map(json_to_node).collect()),
        Value::Object(map) => YamlNode::Map(
            map.iter()
                .map(|(key, item)| (key.clone(), json_to_node(item)))
                .collect(),
        ),
    }
}

/// `repr(float).lower()` with PyYAML's `.0`-before-`e` repair (`repr(1e17)`
/// spells `1e17`, not a valid `!!float`, so the representer emits
/// `1.0e+17`). Shortest
/// round-trip digits match Python's `repr` for ordinary magnitudes; Rust
/// scientific notation omits the exponent sign and padding (`1e300`),
/// which [`normalize_scientific`] restores to Python's form (`1.0e+300`).
fn py_repr_float(value: f64) -> String {
    // Proof the NaN/infinity arms are gone: `Float` nodes only arise from
    // `json_to_node`, where `Number::as_f64` never yields non-finite
    // (serde_json 1.0.151 `from_f64` requires `is_finite`, the parser
    // rejects `1e400` with "number out of range", and `Value::from`
    // maps non-finite to `Null` — so no `Number` is ever NaN or infinite).
    let text = format!("{value:?}");
    if let Some(normalized) = normalize_scientific(&text) {
        return normalized;
    }
    // Proof the old `.0e` repair is gone: every scientific spelling Rust's
    // `Debug` can emit (`1e300`, `1.5e-7`) carries a valid exponent, so
    // `normalize_scientific` returns `Some` for all of them and the repair
    // below it was unreachable.
    text
}

/// Rust `1e300`/`1.5e-7` to Python `1.0e+300`/`1.5e-07`: the mantissa keeps
/// a fraction and the exponent keeps its sign with at least two digits.
fn normalize_scientific(text: &str) -> Option<String> {
    let position = text.find('e')?;
    let (mantissa, exponent) = text.split_at(position);
    let exponent = &exponent[1..];
    let (sign, digits) = match exponent.strip_prefix('-') {
        Some(digits) => ('-', digits),
        None => ('+', exponent.strip_prefix('+').unwrap_or(exponent)),
    };
    // Proof the old digit guard is gone: `text` is always a Rust `Debug`
    // float, whose exponent is never empty and all ASCII digits, so the
    // `return None` below it was unreachable (the only live `None` is the
    // missing-`e` early return above).
    let mut mantissa = mantissa.to_owned();
    if !mantissa.contains('.') {
        mantissa.push_str(".0");
    }
    let digits = if digits.len() < 2 {
        format!("0{digits}")
    } else {
        digits.to_owned()
    };
    Some(format!("{mantissa}e{sign}{digits}"))
}
/// Emitter state for the block path. Columns count Unicode code points,
/// exactly like PyYAML's `len(data)`.
struct YamlEmitter {
    out: String,
    column: usize,
    whitespace: bool,
    indention: bool,
    indent: Option<usize>,
    indents: Vec<Option<usize>>,
}

impl YamlEmitter {
    const BEST_INDENT: usize = 2;
    const BEST_WIDTH: usize = 80;

    fn new() -> Self {
        Self {
            out: String::new(),
            column: 0,
            whitespace: true,
            indention: true,
            indent: None,
            indents: Vec::new(),
        }
    }

    fn write_text(&mut self, text: &str) {
        self.column += text.chars().count();
        self.out.push_str(text);
    }

    fn write_chars(&mut self, text: &[char]) {
        for ch in text {
            self.column += 1;
            self.out.push(*ch);
        }
    }

    /// `write_indicator`: a leading space unless already at whitespace.
    fn write_indicator(&mut self, indicator: &str, need_whitespace: bool, indention: bool) {
        if self.whitespace || !need_whitespace {
            self.write_text(indicator);
        } else {
            self.out.push(' ');
            self.column += 1;
            self.write_text(indicator);
        }
        self.whitespace = false;
        self.indention = self.indention && indention;
    }

    fn write_line_break(&mut self, data: Option<char>) {
        // PyYAML resets the column to zero and writes the break without
        // counting it, so the following `write_indent` pads the full
        // indent (e.g. two spaces after an embedded NEL at top level).
        self.whitespace = true;
        self.indention = true;
        self.column = 0;
        match data {
            None => self.out.push('\n'),
            Some(ch) => {
                self.out.push(ch);
            }
        }
    }

    fn write_indent(&mut self) {
        let indent = self.indent.unwrap_or(0);
        if !self.indention || self.column > indent || (self.column == indent && !self.whitespace) {
            self.write_line_break(None);
        }
        if self.column < indent {
            self.whitespace = true;
            for _ in self.column..indent {
                self.out.push(' ');
            }
            self.column = indent;
        }
    }

    fn increase_indent(&mut self, indentless: bool) {
        self.indents.push(self.indent);
        if self.indent.is_none() {
            // Proof the old `flow` arm is gone: flow indenting only happened
            // in `write_scalar`, which runs solely under an established block
            // indent (`emit_document` opens with `write_map`), so `None`
            // always means the block top level starting at zero.
            self.indent = Some(0);
        } else if !indentless {
            self.indent = Some(self.indent.expect("indent") + Self::BEST_INDENT);
        }
    }

    /// `expect_block_mapping`: one `key: value` pair per line.
    fn write_map(&mut self, pairs: &[(String, YamlNode)]) {
        self.increase_indent(false);
        for (key, value) in pairs {
            self.write_indent();
            if is_simple_key(key) {
                self.write_scalar(key, &YamlTag::Str, true);
                self.write_indicator(":", false, false);
            } else {
                // `check_simple_key` fails only for empty or multiline keys;
                // the `? key` explicit form always breaks before the colon,
                // exactly like `expect_block_mapping_value`.
                self.write_indicator("?", true, true);
                self.write_scalar(key, &YamlTag::Str, false);
                self.write_indent();
                self.write_indicator(":", true, true);
            }
            self.write_value(value, true);
        }
        self.indent = self.indents.pop().expect("indent stack");
    }

    /// `expect_block_sequence`, indentless for mapping values (`- key`
    /// beside the parent key) and indented for nested sequences.
    fn write_seq(&mut self, items: &[YamlNode], mapping_value: bool) {
        self.increase_indent(mapping_value);
        for item in items {
            self.write_indent();
            self.write_indicator("-", true, true);
            match item {
                YamlNode::Map(pairs) => self.write_map(pairs),
                YamlNode::Seq(nested) if !nested.is_empty() => self.write_seq(nested, false),
                other => self.write_value(other, false),
            }
        }
        self.indent = self.indents.pop().expect("indent stack");
    }

    fn write_value(&mut self, node: &YamlNode, mapping_value: bool) {
        match node {
            YamlNode::Str(text) => self.write_scalar(text, &YamlTag::Str, false),
            YamlNode::Int(number) => self.write_scalar(&number.to_string(), &YamlTag::Int, false),
            YamlNode::Bool(flag) => {
                self.write_scalar(if *flag { "true" } else { "false" }, &YamlTag::Bool, false);
            }
            YamlNode::Null => self.write_scalar("null", &YamlTag::Null, false),
            YamlNode::Float(float) => {
                self.write_scalar(&py_repr_float(*float), &YamlTag::Float, false);
            }
            YamlNode::Seq(items) if items.is_empty() => self.write_indicator("[]", true, false),
            YamlNode::Seq(items) => self.write_seq(items, mapping_value),
            YamlNode::Map(pairs) if pairs.is_empty() => self.write_indicator("{}", true, false),
            YamlNode::Map(pairs) => self.write_map(pairs),
        }
    }

    /// `expect_scalar`: scalars wrap at `indent+2`, which is where plain
    /// and single-quoted continuation lines get their two spaces. `split`
    /// is false for simple keys.
    fn write_scalar(&mut self, text: &str, tag: &YamlTag, simple_key: bool) {
        let chars: Vec<char> = text.chars().collect();
        let analysis = analyze_scalar(&chars);
        let style = choose_scalar_style(*tag, &chars, &analysis, simple_key);
        self.increase_indent(false);
        let split = !simple_key;
        match style {
            ScalarStyle::Plain => self.write_plain(&chars, split),
            ScalarStyle::Single => self.write_single_quoted(&chars, split),
            ScalarStyle::Double => self.write_double_quoted(&chars, split),
        }
        self.indent = self.indents.pop().expect("indent stack");
    }

    /// `write_plain`: single interior spaces are the only split points; a
    /// space past `best_width` starts a continuation line.
    fn write_plain(&mut self, text: &[char], split: bool) {
        // Proof the empty early-return is gone: empty text resolves `Null`,
        // never `Str`, so `choose_scalar_style` never selects plain for it
        // (`''` covers the empty shape instead). Proof the `breaks` branch is
        // gone: any `is_line_break` char sets `line_breaks`, which refuses
        // both plain styles in `analyze_scalar`, so plain text never carries
        // a break into this loop.
        if !self.whitespace {
            self.out.push(' ');
            self.column += 1;
        }
        self.whitespace = false;
        self.indention = false;
        let mut spaces = false;
        let mut start = 0;
        let mut end = 0;
        while end <= text.len() {
            let ch = if end < text.len() {
                Some(text[end])
            } else {
                None
            };
            if spaces {
                if ch != Some(' ') {
                    if start + 1 == end && self.column > Self::BEST_WIDTH && split {
                        self.write_indent();
                        self.whitespace = false;
                        self.indention = false;
                    } else {
                        self.write_chars(&text[start..end]);
                    }
                    start = end;
                }
            } else if ch.is_none() || ch == Some(' ') || ch.is_some_and(is_line_break) {
                self.write_chars(&text[start..end]);
                start = end;
            }
            if let Some(ch) = ch {
                spaces = ch == ' ';
            }
            end += 1;
        }
    }

    /// `write_single_quoted`: splits at interior single spaces; an embedded
    /// line break ends the line, re-emits the break (hence one blank line
    /// per `\n`), and re-indents. `''` doubles a quote.
    fn write_single_quoted(&mut self, text: &[char], split: bool) {
        self.write_indicator("'", true, false);
        let mut spaces = false;
        let mut breaks = false;
        let mut start = 0;
        let mut end = 0;
        while end <= text.len() {
            let ch = if end < text.len() {
                Some(text[end])
            } else {
                None
            };
            if spaces {
                if ch != Some(' ') {
                    if start + 1 == end
                        && self.column > Self::BEST_WIDTH
                        && split
                        && start != 0
                        && end != text.len()
                    {
                        self.write_indent();
                    } else {
                        self.write_chars(&text[start..end]);
                    }
                    start = end;
                }
            } else if breaks {
                if ch.is_none() || ch.is_some_and(|ch| !is_line_break(ch)) {
                    if text[start] == '\n' {
                        self.write_line_break(None);
                    }
                    for br in &text[start..end] {
                        if *br == '\n' {
                            self.write_line_break(None);
                        } else {
                            self.write_line_break(Some(*br));
                        }
                    }
                    self.write_indent();
                    start = end;
                }
            } else if (ch.is_none()
                || ch == Some(' ')
                || ch.is_some_and(is_line_break)
                || ch == Some('\''))
                && start < end
            {
                self.write_chars(&text[start..end]);
                start = end;
            }
            if ch == Some('\'') {
                self.column += 2;
                self.out.push_str("''");
                start = end + 1;
            }
            if let Some(ch) = ch {
                spaces = ch == ' ';
                breaks = is_line_break(ch);
            }
            end += 1;
        }
        self.write_indicator("'", false, false);
    }

    /// `write_double_quoted` with `ESCAPE_REPLACEMENTS` and backslash
    /// continuations past `best_width`.
    fn write_double_quoted(&mut self, text: &[char], split: bool) {
        self.write_indicator("\"", true, false);
        let mut start = 0;
        let mut end = 0;
        while end <= text.len() {
            let ch = if end < text.len() {
                Some(text[end])
            } else {
                None
            };
            if ch.is_none()
                || ch.is_some_and(|ch| {
                    ch == '"'
                        || ch == '\\'
                        || ch == '\u{85}'
                        || ch == ' '
                        || ch == ' '
                        || ch == '﻿'
                        || !(('\x20'..='\x7e').contains(&ch)
                            || ('\u{a0}'..='\u{d7ff}').contains(&ch)
                            || ('\u{e000}'..='\u{fffd}').contains(&ch))
                })
            {
                if start < end {
                    self.write_chars(&text[start..end]);
                    start = end;
                }
                if let Some(ch) = ch {
                    let escaped = match escape_replacement(ch) {
                        Some(replacement) => format!("\\{replacement}"),
                        None => {
                            let code = ch as u32;
                            if code <= 0xff {
                                format!("\\x{code:02X}")
                            } else if code <= 0xffff {
                                format!("\\u{code:04X}")
                            } else {
                                format!("\\U{code:08X}")
                            }
                        }
                    };
                    self.column += escaped.chars().count();
                    self.out.push_str(&escaped);
                    start = end + 1;
                }
            }
            // Right after an escape `start` is `end + 1`: Python slices the
            // inverted window to `''`, so the subtraction saturates and the
            // slice falls back to empty exactly like `text[start:end]`.
            if 0 < end
                && end + 1 < text.len()
                && (ch == Some(' ') || start >= end)
                && self.column + end.saturating_sub(start) > Self::BEST_WIDTH
                && split
            {
                let mut data: String = if start <= end {
                    text[start..end].iter().collect()
                } else {
                    String::new()
                };
                data.push('\\');
                if start < end {
                    start = end;
                }
                self.column += data.chars().count();
                self.out.push_str(&data);
                self.write_indent();
                self.whitespace = false;
                self.indention = false;
                if text[start] == ' ' {
                    self.out.push('\\');
                    self.column += 1;
                }
            }
            end += 1;
        }
        self.write_indicator("\"", false, false);
    }

    /// The document: one implicit block mapping plus the closing line break
    /// (`expect_document_end` → `write_indent`); no `---` markers, since
    /// `render_markdown` adds the file's own fences.
    fn emit_document(mut self, pairs: &[(String, YamlNode)]) -> String {
        self.write_map(pairs);
        self.write_indent();
        self.out
    }
}

fn is_line_break(ch: char) -> bool {
    matches!(ch, '\n' | '\u{85}' | ' ' | ' ')
}

/// `check_simple_key`: scalar keys under 128 characters that are neither
/// empty nor multiline ride on the `key: value` line.
fn is_simple_key(key: &str) -> bool {
    let chars: Vec<char> = key.chars().collect();
    chars.len() < 128 && !chars.is_empty() && !chars.iter().any(|ch| is_line_break(*ch))
}

fn escape_replacement(ch: char) -> Option<&'static str> {
    match ch {
        '\0' => Some("0"),
        '\x07' => Some("a"),
        '\x08' => Some("b"),
        '\t' => Some("t"),
        '\n' => Some("n"),
        '\x0b' => Some("v"),
        '\x0c' => Some("f"),
        '\r' => Some("r"),
        '\x1b' => Some("e"),
        '"' => Some("\""),
        '\\' => Some("\\"),
        '\u{85}' => Some("N"),
        // Proof the NBSP `_` arm is gone: U+00A0 is printable under
        // `allow_unicode` (see `analyze_scalar`), so double-quoted output
        // passes it through literally and never asks for its replacement.
        ' ' => Some("L"),
        ' ' => Some("P"),
        _ => None,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ScalarAnalysis {
    empty: bool,
    multiline: bool,
    allow_flow_plain: bool,
    allow_block_plain: bool,
    allow_single_quoted: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ScalarStyle {
    Plain,
    Single,
    Double,
}

/// `Emitter.analyze_scalar`, character for character. `allow_unicode` is
/// always true here (`safe_dump(allow_unicode=True)`), so printable
/// non-ASCII never forces double quotes.
fn analyze_scalar(text: &[char]) -> ScalarAnalysis {
    if text.is_empty() {
        return ScalarAnalysis {
            empty: true,
            multiline: false,
            allow_flow_plain: false,
            allow_block_plain: true,
            allow_single_quoted: true,
        };
    }
    let mut block_indicators = false;
    let mut flow_indicators = false;
    let mut line_breaks = false;
    let mut special_characters = false;
    let mut leading_space = false;
    let mut leading_break = false;
    let mut trailing_space = false;
    let mut trailing_break = false;
    let mut break_space = false;
    let mut space_break = false;
    if text.len() >= 3
        && ((text[0] == '-' && text[1] == '-' && text[2] == '-')
            || (text[0] == '.' && text[1] == '.' && text[2] == '.'))
    {
        block_indicators = true;
        flow_indicators = true;
    }
    let mut preceded_by_whitespace = true;
    let mut followed_by_whitespace = text.len() == 1 || is_follow_whitespace(text[1]);
    let mut previous_space = false;
    let mut previous_break = false;
    let mut index = 0;
    while index < text.len() {
        let ch = text[index];
        if index == 0 {
            if matches!(
                ch,
                '#' | ','
                    | '['
                    | ']'
                    | '{'
                    | '}'
                    | '&'
                    | '*'
                    | '!'
                    | '|'
                    | '>'
                    | '\''
                    | '"'
                    | '%'
                    | '@'
                    | '`'
            ) {
                flow_indicators = true;
                block_indicators = true;
            }
            if matches!(ch, '?' | ':') {
                flow_indicators = true;
                if followed_by_whitespace {
                    block_indicators = true;
                }
            }
            if ch == '-' && followed_by_whitespace {
                flow_indicators = true;
                block_indicators = true;
            }
        } else {
            if matches!(ch, ',' | '?' | '[' | ']' | '{' | '}') {
                flow_indicators = true;
            }
            if ch == ':' {
                flow_indicators = true;
                if followed_by_whitespace {
                    block_indicators = true;
                }
            }
            if ch == '#' && preceded_by_whitespace {
                flow_indicators = true;
                block_indicators = true;
            }
        }
        if is_line_break(ch) {
            line_breaks = true;
        }
        if !(ch == '\n' || ('\x20'..='\x7e').contains(&ch)) {
            if (ch == '\u{85}'
                || ('\u{a0}'..='\u{d7ff}').contains(&ch)
                || ('\u{e000}'..='\u{fffd}').contains(&ch)
                || ('\u{10000}'..'\u{10ffff}').contains(&ch))
                && ch != '﻿'
            {
                // Printable non-ASCII: fine under `allow_unicode=True`.
            } else {
                special_characters = true;
            }
        }
        if ch == ' ' {
            if index == 0 {
                leading_space = true;
            }
            if index == text.len() - 1 {
                trailing_space = true;
            }
            if previous_break {
                break_space = true;
            }
            previous_space = true;
            previous_break = false;
        } else if is_line_break(ch) {
            if index == 0 {
                leading_break = true;
            }
            if index == text.len() - 1 {
                trailing_break = true;
            }
            if previous_space {
                space_break = true;
            }
            previous_space = false;
            previous_break = true;
        } else {
            previous_space = false;
            previous_break = false;
        }
        index += 1;
        preceded_by_whitespace = is_follow_whitespace(ch);
        followed_by_whitespace = index + 1 >= text.len() || is_follow_whitespace(text[index + 1]);
    }
    let _ = preceded_by_whitespace;
    let mut allow_flow_plain = true;
    let mut allow_block_plain = true;
    let mut allow_single_quoted = true;
    if leading_space || leading_break || trailing_space || trailing_break {
        allow_flow_plain = false;
        allow_block_plain = false;
    }
    if break_space {
        allow_flow_plain = false;
        allow_block_plain = false;
        allow_single_quoted = false;
    }
    if space_break || special_characters {
        allow_flow_plain = false;
        allow_block_plain = false;
        allow_single_quoted = false;
    }
    if line_breaks {
        allow_flow_plain = false;
        allow_block_plain = false;
    }
    if flow_indicators {
        allow_flow_plain = false;
    }
    if block_indicators {
        allow_block_plain = false;
    }
    ScalarAnalysis {
        empty: false,
        multiline: line_breaks,
        allow_flow_plain,
        allow_block_plain,
        allow_single_quoted,
    }
}

fn is_follow_whitespace(ch: char) -> bool {
    matches!(ch, '\0' | ' ' | '\t' | '\r' | '\n' | '\u{85}' | ' ' | ' ')
}

/// `Resolver.resolve` for scalar nodes: the tag a plain scalar with this
/// value would resolve to. Only `Str` can refuse the plain style here —
/// every represented non-string spells its own tag.
fn resolve_tag(text: &[char]) -> YamlTag {
    if text.is_empty() {
        return YamlTag::Null;
    }
    match text[0] {
        'y' | 'Y' | 't' | 'T' | 'f' | 'F' | 'o' | 'O' => {
            if is_bool_literal(text) {
                YamlTag::Bool
            } else {
                YamlTag::Str
            }
        }
        'n' | 'N' => {
            if is_bool_literal(text) {
                YamlTag::Bool
            } else if is_null_literal(text) {
                YamlTag::Null
            } else {
                YamlTag::Str
            }
        }
        '~' => YamlTag::Null,
        '<' => {
            if text.len() == 2 && text[0] == '<' && text[1] == '<' {
                YamlTag::Merge
            } else {
                YamlTag::Str
            }
        }
        '=' => {
            if text.len() == 1 {
                YamlTag::Value
            } else {
                YamlTag::Str
            }
        }
        '-' | '+' | '0'..='9' | '.' => {
            if is_int_literal(text) {
                YamlTag::Int
            } else if is_float_literal(text) {
                YamlTag::Float
            } else if is_timestamp_literal(text) {
                YamlTag::Timestamp
            } else {
                YamlTag::Str
            }
        }
        _ => YamlTag::Str,
    }
}

/// `choose_scalar_style` for untagged, non-canonical block scalars: plain
/// when the value resolves back to its tag and the analysis allows, else
/// single-quoted, else double-quoted.
fn choose_scalar_style(
    tag: YamlTag,
    text: &[char],
    analysis: &ScalarAnalysis,
    simple_key_context: bool,
) -> ScalarStyle {
    let implicit = tag == resolve_tag(text);
    if implicit
        && !(simple_key_context && (analysis.empty || analysis.multiline))
        && analysis.allow_block_plain
    {
        return ScalarStyle::Plain;
    }
    if analysis.allow_single_quoted && !(simple_key_context && analysis.multiline) {
        return ScalarStyle::Single;
    }
    ScalarStyle::Double
}

fn is_bool_literal(text: &[char]) -> bool {
    const WORDS: &[&str] = &[
        "yes", "Yes", "YES", "no", "No", "NO", "true", "True", "TRUE", "false", "False", "FALSE",
        "on", "On", "ON", "off", "Off", "OFF",
    ];
    let text: String = text.iter().collect();
    WORDS.contains(&text.as_str())
}

fn is_null_literal(text: &[char]) -> bool {
    text.is_empty()
        || (text.len() == 1 && text[0] == '~')
        || (text.len() == 4 && text[0] == 'n' && text[1] == 'u' && text[2] == 'l' && text[3] == 'l')
        || (text.len() == 4 && text[0] == 'N' && text[1] == 'u' && text[2] == 'l' && text[3] == 'l')
        || (text.len() == 4 && text[0] == 'N' && text[1] == 'U' && text[2] == 'L' && text[3] == 'L')
}

fn strip_sign(text: &[char]) -> &[char] {
    if text.first().is_some_and(|ch| matches!(ch, '+' | '-')) {
        &text[1..]
    } else {
        text
    }
}

fn is_digits_underscore(text: &[char]) -> bool {
    !text.is_empty() && text.iter().all(|ch| matches!(ch, '0'..='9' | '_'))
}

/// The `!!int` implicit pattern, literally: binaries, octals, decimals
/// (with underscores), hex, and sexagesimals.
fn is_int_literal(text: &[char]) -> bool {
    let body = strip_sign(text);
    if body.len() > 2 && body[0] == '0' && body[1] == 'b' {
        return body[2..].iter().all(|ch| matches!(ch, '0' | '1' | '_'));
    }
    if body.len() > 2 && body[0] == '0' && body[1] == 'x' {
        return body[2..]
            .iter()
            .all(|ch| matches!(ch, '0'..='9' | 'a'..='f' | 'A'..='F' | '_'));
    }
    if body.len() > 1 && body[0] == '0' {
        return body[1..].iter().all(|ch| matches!(ch, '0'..='7' | '_'));
    }
    if body.len() == 1 && body[0] == '0' {
        return true;
    }
    if body.first().is_some_and(|ch| matches!(ch, '1'..='9')) {
        if is_digits_underscore(body) {
            return true;
        }
        // `[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+`
        let mut groups = 0;
        let mut cursor = 0;
        while cursor < body.len() && matches!(body[cursor], '0'..='9' | '_') {
            cursor += 1;
        }
        loop {
            if cursor >= body.len() || body[cursor] != ':' {
                return false;
            }
            cursor += 1;
            if cursor < body.len()
                && matches!(body[cursor], '0'..='5')
                && cursor + 1 < body.len()
                && body[cursor + 1].is_ascii_digit()
            {
                cursor += 2;
            } else if cursor < body.len() && body[cursor].is_ascii_digit() {
                cursor += 1;
            } else {
                return false;
            }
            groups += 1;
            if cursor == body.len() {
                return groups > 0;
            }
            if !matches!(body[cursor], '0'..='9' | '_' | ':') {
                return false;
            }
            while cursor < body.len() && matches!(body[cursor], '0'..='9' | '_') {
                cursor += 1;
            }
        }
    }
    false
}

/// `[eE][-+][0-9]+`: the sign is required.
fn is_exponent_suffix(text: &[char]) -> bool {
    text.len() >= 3
        && matches!(text[0], 'e' | 'E')
        && matches!(text[1], '+' | '-')
        && !text[2..].is_empty()
        && text[2..].iter().all(|ch| ch.is_ascii_digit())
}

/// The `!!float` implicit pattern, literally.
fn is_float_literal(text: &[char]) -> bool {
    // `[-+]?\.(?:inf|Inf|INF)`
    let signed = strip_sign(text);
    if signed.len() == 4
        && signed[0] == '.'
        && ((signed[1] == 'i' && signed[2] == 'n' && signed[3] == 'f')
            || (signed[1] == 'I' && signed[2] == 'n' && signed[3] == 'f')
            || (signed[1] == 'I' && signed[2] == 'N' && signed[3] == 'F'))
    {
        return true;
    }
    // `\.(?:nan|NaN|NAN)`, unsigned only.
    if text.len() == 4
        && text[0] == '.'
        && ((text[1] == 'n' && text[2] == 'a' && text[3] == 'n')
            || (text[1] == 'N' && text[2] == 'a' && text[3] == 'N')
            || (text[1] == 'N' && text[2] == 'A' && text[3] == 'N'))
    {
        return true;
    }
    // `\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?`, unsigned only.
    if text.first() == Some(&'.') && text.get(1).is_some_and(|ch| ch.is_ascii_digit()) {
        let mut index = 2;
        while index < text.len() && matches!(text[index], '0'..='9' | '_') {
            index += 1;
        }
        if index == text.len() {
            return true;
        }
        return matches!(text[index], 'e' | 'E') && is_exponent_suffix(&text[index..]);
    }
    // With an optional sign: plain decimals with fraction/exponent first, then sexagesimal floats.
    let mut index = 0;
    if text.get(index).is_some_and(|ch| matches!(ch, '+' | '-')) {
        index += 1;
    }
    let digits_start = index;
    while index < text.len() && matches!(text[index], '0'..='9' | '_') {
        index += 1;
    }
    if index == digits_start || !text[digits_start].is_ascii_digit() {
        return false;
    }
    if index < text.len() && text[index] == '.' {
        // `[-+]?[0-9][0-9_]*\.[0-9_]*(?:[eE][-+][0-9]+)?`
        index += 1;
        while index < text.len() && matches!(text[index], '0'..='9' | '_') {
            index += 1;
        }
        if index == text.len() {
            return true;
        }
        return matches!(text[index], 'e' | 'E') && is_exponent_suffix(&text[index..]);
    }
    // `[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*`
    let mut groups = 0;
    while index < text.len() && text[index] == ':' {
        index += 1;
        if index < text.len()
            && matches!(text[index], '0'..='5')
            && index + 1 < text.len()
            && text[index + 1].is_ascii_digit()
        {
            index += 2;
        } else if index < text.len() && text[index].is_ascii_digit() {
            index += 1;
        } else {
            return false;
        }
        groups += 1;
    }
    if groups == 0 || index >= text.len() || text[index] != '.' {
        return false;
    }
    index += 1;
    while index < text.len() && matches!(text[index], '0'..='9' | '_') {
        index += 1;
    }
    index == text.len()
}

/// The `!!timestamp` implicit pattern, syntactically: a bare date or a
/// datetime with its optional fraction and zone.
fn is_timestamp_literal(text: &[char]) -> bool {
    let text: String = text.iter().collect();
    let bytes = text.as_bytes();
    if text.len() == 10
        && bytes[0].is_ascii_digit()
        && bytes[1].is_ascii_digit()
        && bytes[2].is_ascii_digit()
        && bytes[3].is_ascii_digit()
        && bytes[4] == b'-'
        && bytes[5].is_ascii_digit()
        && bytes[6].is_ascii_digit()
        && bytes[7] == b'-'
        && bytes[8].is_ascii_digit()
        && bytes[9].is_ascii_digit()
    {
        return true;
    }
    let mut index = 0;
    let take_digits = |index: &mut usize, min: usize, max: usize| -> bool {
        let start = *index;
        while *index < text.len() && bytes[*index].is_ascii_digit() && *index - start < max {
            *index += 1;
        }
        let width = *index - start;
        width >= min && width <= max
    };
    if !take_digits(&mut index, 4, 4) || bytes.get(index) != Some(&b'-') {
        return false;
    }
    index += 1;
    if !take_digits(&mut index, 1, 2) || bytes.get(index) != Some(&b'-') {
        return false;
    }
    index += 1;
    if !take_digits(&mut index, 1, 2) {
        return false;
    }
    if bytes.get(index).is_some_and(|b| matches!(b, b't' | b'T')) {
        index += 1;
    } else {
        let start = index;
        while bytes.get(index).is_some_and(|b| matches!(b, b' ' | b'\t')) {
            index += 1;
        }
        if index == start {
            return false;
        }
    }
    if !take_digits(&mut index, 1, 2) || bytes.get(index) != Some(&b':') {
        return false;
    }
    index += 1;
    if !take_digits(&mut index, 2, 2) || bytes.get(index) != Some(&b':') {
        return false;
    }
    index += 1;
    if !take_digits(&mut index, 2, 2) {
        return false;
    }
    if bytes.get(index) == Some(&b'.') {
        index += 1;
        while bytes.get(index).is_some_and(|b| b.is_ascii_digit()) {
            index += 1;
        }
    }
    if index == text.len() {
        return true;
    }
    while bytes.get(index).is_some_and(|b| matches!(b, b' ' | b'\t')) {
        index += 1;
    }
    if index == text.len() {
        return true;
    }
    if bytes.get(index) == Some(&b'Z') {
        return index + 1 == text.len();
    }
    if bytes.get(index).is_some_and(|b| matches!(b, b'+' | b'-')) {
        index += 1;
        if !take_digits(&mut index, 1, 2) {
            return false;
        }
        if bytes.get(index) == Some(&b':') {
            index += 1;
            if !take_digits(&mut index, 2, 2) {
                return false;
            }
        }
        return index == text.len();
    }
    false
}

// --- `int(level)` coercion --------------------------------------------------
//
// Export clamps heading levels with `min(max(int(level), 1), 6)` where
// `level` is `attributes.get("level", 2)`. The coercion below mirrors
// CPython's `int()` over the JSON shapes row attributes can hold: bools
// are 0/1, floats truncate toward zero, strings parse with surrounding
// whitespace, one sign, and underscores only between digits.

fn py_int_from_value(value: &Value) -> Result<i64, Error> {
    match value {
        Value::Null => Err(Error::Validation(
            "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"
                .to_owned(),
        )),
        Value::Bool(flag) => Ok(i64::from(*flag)),
        Value::Number(number) => {
            if let Some(int) = number.as_i64() {
                Ok(int)
            } else if let Some(uint) = number.as_u64() {
                Ok(uint.min(i64::MAX as u64) as i64)
            } else {
                // Proof the NaN/infinity arms and the final `Err` else are
                // gone: without `arbitrary_precision` the int arms leave only
                // f64, so `as_f64` is infallible here (`expect` documents the
                // tail), and non-finite is unconstructible (serde_json 1.0.151
                // `from_f64` requires `is_finite`, the parser rejects `1e400`
                // with "number out of range", `Value::from` maps non-finite
                // to `Null`) — the surviving float is always finite.
                let float = number
                    .as_f64()
                    .expect("serde_json Number is always u64/i64/f64");
                let truncated = float.trunc();
                if truncated >= i64::MAX as f64 {
                    Ok(i64::MAX)
                } else if truncated <= i64::MIN as f64 {
                    Ok(i64::MIN)
                } else {
                    Ok(truncated as i64)
                }
            }
        }
        Value::String(text) => py_int_from_str(text),
        Value::Array(_) => Err(Error::Validation(
            "int() argument must be a string, a bytes-like object or a real number, not 'list'"
                .to_owned(),
        )),
        Value::Object(_) => Err(Error::Validation(
            "int() argument must be a string, a bytes-like object or a real number, not 'dict'"
                .to_owned(),
        )),
    }
}

/// `int(str)`: surrounding whitespace goes, one sign, digits (ASCII or
/// otherwise decimal) with underscores only between digits. Saturates past
/// `i64` — the caller clamps to 1..=6, so saturation and bignum agree
/// downstream.
fn py_int_from_str(text: &str) -> Result<i64, Error> {
    let invalid = || {
        Error::Validation(format!(
            "invalid literal for int() with base 10: {}",
            py_repr_str(text)
        ))
    };
    let stripped = py_strip(text);
    let mut chars = stripped.chars().peekable();
    let negative = match chars.peek() {
        Some('+') => {
            chars.next();
            false
        }
        Some('-') => {
            chars.next();
            true
        }
        _ => false,
    };
    let mut digits: Vec<u32> = Vec::new();
    let mut previous_underscore = false;
    for ch in chars {
        if ch == '_' {
            if digits.is_empty() || previous_underscore {
                return Err(invalid());
            }
            previous_underscore = true;
            continue;
        }
        match ch.to_digit(10) {
            Some(digit) => {
                digits.push(digit);
                previous_underscore = false;
            }
            None => return Err(invalid()),
        }
    }
    if digits.is_empty() || previous_underscore {
        return Err(invalid());
    }
    let mut magnitude: i128 = 0;
    for digit in digits {
        magnitude = magnitude
            .saturating_mul(10)
            .saturating_add(i128::from(digit));
        if magnitude > i128::from(i64::MAX) + 1 {
            break;
        }
    }
    if negative {
        if magnitude > i128::from(i64::MAX) {
            Ok(i64::MIN)
        } else {
            Ok(-(magnitude as i64))
        }
    } else if magnitude >= i128::from(i64::MAX) {
        Ok(i64::MAX)
    } else {
        Ok(magnitude as i64)
    }
}

/// Heading level with `int()` coercion and the 1..=6 clamp.
fn heading_level(attributes: &Map<String, Value>) -> Result<i64, Error> {
    let level = match attributes.get("level") {
        None => 2,
        Some(value) => py_int_from_value(value)?,
    };
    Ok(level.clamp(1, 6))
}

fn revision_state_value(state: &RevisionState) -> &'static str {
    match state {
        RevisionState::Draft => "draft",
        RevisionState::Frozen => "frozen",
        RevisionState::Published => "published",
        RevisionState::Superseded => "superseded",
    }
}

fn intent_value(intent: &Intent) -> &'static str {
    match intent {
        Intent::Quotation => "quotation",
        Intent::Translation => "translation",
        Intent::Support => "support",
        Intent::Contrast => "contrast",
        Intent::Background => "background",
        Intent::Definition => "definition",
        Intent::Source => "source",
        Intent::SeeAlso => "see_also",
    }
}

/// The §6.4 file: front matter with citations in block order, then blocks.
pub fn render_markdown(view: &AssembledRevision) -> String {
    let mut front: Vec<(String, YamlNode)> = vec![
        ("work".to_owned(), YamlNode::Str(view.work.slug.clone())),
        ("title".to_owned(), YamlNode::Str(view.work.title.clone())),
        (
            "type".to_owned(),
            YamlNode::Str(view.work.work_type.clone()),
        ),
        (
            "revision".to_owned(),
            YamlNode::Int(view.revision.revision_number),
        ),
        (
            "state".to_owned(),
            YamlNode::Str(revision_state_value(&view.revision.state).to_owned()),
        ),
    ];
    let mut entries: Vec<YamlNode> = Vec::new();
    for item in &view.blocks {
        for entry in &item.citations {
            for row in &entry.items {
                let span = row.source_span_id.and_then(|id| view.spans.get(&id));
                let mut cited = vec![
                    (
                        "key".to_owned(),
                        YamlNode::Str(entry.occurrence.citation_key.to_string()),
                    ),
                    (
                        "intent".to_owned(),
                        YamlNode::Str(intent_value(&entry.occurrence.intent).to_owned()),
                    ),
                ];
                if let Some(span) = span {
                    cited.push((
                        "document_id".to_owned(),
                        YamlNode::Str(span.document_id.to_string()),
                    ));
                    cited.push(("char_start".to_owned(), YamlNode::Int(span.char_start)));
                    cited.push(("char_end".to_owned(), YamlNode::Int(span.char_end)));
                }
                if let Some(quoted) = &row.quoted_text {
                    cited.push(("quoted_text".to_owned(), YamlNode::Str(quoted.clone())));
                }
                if let Some(status) = &row.verify_status {
                    cited.push(("verify_status".to_owned(), YamlNode::Str(status.clone())));
                }
                if let Some(edition_key) = &row.edition_key {
                    cited.push(("edition_key".to_owned(), YamlNode::Str(edition_key.clone())));
                }
                if let Some(edition_id) = &row.edition_id {
                    cited.push((
                        "edition_id".to_owned(),
                        YamlNode::Str(edition_id.to_string()),
                    ));
                }
                if !row.locator.is_empty() {
                    // `dict(row.locator)`: insertion order rides along, matching
                    // Python dict order (file order from YAML, document order
                    // from JSON).
                    cited.push((
                        "locator".to_owned(),
                        YamlNode::Map(
                            row.locator
                                .iter()
                                .map(|(key, value)| (key.clone(), json_to_node(value)))
                                .collect(),
                        ),
                    ));
                }
                entries.push(YamlNode::Map(cited));
            }
        }
    }
    if !entries.is_empty() {
        front.push(("citations".to_owned(), YamlNode::Seq(entries)));
    }
    // `yaml.safe_dump(front, sort_keys=False, allow_unicode=True).rstrip()`.
    let dumped = YamlEmitter::new().emit_document(&front);
    let dumped = dumped.trim_end_matches(py_is_space);
    let mut parts = vec![
        "---".to_owned(),
        dumped.to_owned(),
        "---".to_owned(),
        String::new(),
    ];
    for item in &view.blocks {
        let block = &item.block;
        parts.push(format!("<!-- block:{} -->", block.block_key));
        if block.block_type == "heading" {
            // `int(level)` raises out of the export on corrupt rows exactly
            // like the Python; `expect` documents writer-owned levels coerce.
            let level = heading_level(&block.attributes).expect("heading level coerces to int");
            let hashes = "#".repeat(level as usize);
            let title = block.title.clone().unwrap_or_default();
            parts.push(
                format!("{hashes} {title}")
                    .trim_end_matches(py_is_space)
                    .to_owned(),
            );
            if !block.body_markdown.is_empty() {
                parts.push(block.body_markdown.clone());
            }
        } else {
            if block
                .title
                .as_deref()
                .is_some_and(|title| !title.is_empty())
            {
                parts.push(format!(
                    "<!-- title: {} -->",
                    block.title.clone().unwrap_or_default()
                ));
            }
            parts.push(block.body_markdown.clone());
        }
        for entry in &item.citations {
            if entry.occurrence.placement != Placement::BlockEnd {
                continue;
            }
            let marker = format_marker(&entry.occurrence.citation_key);
            if !block.body_markdown.contains(&marker) {
                parts.push(marker);
            }
        }
        parts.push(String::new());
    }
    parts.join("\n")
}

/// Split edited markdown into front matter and §5.2 blocks.
pub fn parse_markdown(markdown: &str) -> Result<(Map<String, Value>, Vec<ParsedBlock>), Error> {
    let front = FRONT_MATTER_RE.captures(markdown).and_then(|captures| {
        // `match.end()`: the body starts after the whole match, not after
        // group 1 (which ends before the closing fence).
        captures.get(0).map(|whole| {
            (
                captures
                    .get(1)
                    .expect("front text group")
                    .as_str()
                    .to_owned(),
                whole.end(),
            )
        })
    });
    let Some((front_text, front_end)) = front else {
        return Err(Error::Validation(
            "Import needs YAML front matter between --- lines".to_owned(),
        ));
    };
    // `yaml.safe_load(...) or {}`: blank front matter is an empty mapping.
    // `serde_yaml` covers the exporter shapes; exotic YAML (anchors, YAML
    // timestamps as dates) may load looser than PyYAML — only
    // `front["work"]` guides the import, so the mapping check below is the
    // contract.
    let parsed_front: Map<String, Value> = if front_text.trim().is_empty() {
        Map::new()
    } else {
        match serde_yaml::from_str::<serde_yaml::Value>(&front_text) {
            Ok(serde_yaml::Value::Null) => Map::new(),
            Ok(serde_yaml::Value::Mapping(mapping)) => {
                let mut map = Map::new();
                for (key, value) in mapping {
                    map.insert(yaml_key_to_string(&key), yaml_to_json(value));
                }
                map
            }
            Ok(_) => {
                return Err(Error::Validation(
                    "Front matter must be a mapping".to_owned(),
                ));
            }
            Err(error) => {
                return Err(Error::Validation(format!(
                    "Invalid YAML front matter: {error}"
                )));
            }
        }
    };
    let body = &markdown[front_end..];
    let (_, blocks) = parse_blocks(&parsed_front, body)?;
    Ok((parsed_front, blocks))
}

fn yaml_key_to_string(key: &serde_yaml::Value) -> String {
    match key {
        serde_yaml::Value::Null => "null".to_owned(),
        serde_yaml::Value::Bool(flag) => flag.to_string(),
        serde_yaml::Value::Number(number) => number.to_string(),
        serde_yaml::Value::String(text) => text.clone(),
        other => yaml_to_json(other.clone()).to_string(),
    }
}

fn yaml_to_json(value: serde_yaml::Value) -> Value {
    match value {
        serde_yaml::Value::Null => Value::Null,
        serde_yaml::Value::Bool(flag) => Value::Bool(flag),
        serde_yaml::Value::Number(number) => {
            if let Some(uint) = number.as_u64() {
                Value::from(uint)
            } else if let Some(int) = number.as_i64() {
                Value::from(int)
            } else {
                // Proof the old `String` fallback is gone: a serde_yaml
                // `Number` is always u64/i64/f64, so after the int arms only
                // the f64 case remains and `expect` documents the tail.
                Value::from(
                    number
                        .as_f64()
                        .expect("serde_yaml Number is always u64/i64/f64"),
                )
            }
        }
        serde_yaml::Value::String(text) => Value::String(text),
        serde_yaml::Value::Sequence(items) => {
            Value::Array(items.into_iter().map(yaml_to_json).collect())
        }
        serde_yaml::Value::Mapping(mapping) => {
            let mut map = Map::new();
            for (key, item) in mapping {
                map.insert(yaml_key_to_string(&key), yaml_to_json(item));
            }
            Value::Object(map)
        }
        serde_yaml::Value::Tagged(tagged) => yaml_to_json(tagged.value),
    }
}

/// The §5.2 block scanner. Take-semantics for pending keys/titles:
/// `flush_paragraph` consumes them only when it emits a paragraph, so
/// consecutive block comments overwrite the pending key, a title comment
/// survives headings (which never take the pending title), and blank
/// accumulations vanish without consuming anything.
fn parse_blocks(
    _front: &Map<String, Value>,
    body: &str,
) -> Result<(Map<String, Value>, Vec<ParsedBlock>), Error> {
    let mut parsed: Vec<ParsedBlock> = Vec::new();
    let mut heading_stack: Vec<(i64, usize)> = Vec::new();
    let mut pending_key: Option<Uuid> = None;
    let mut pending_title: Option<String> = None;
    let lines = py_splitlines(body);
    let mut index = 0;
    let mut current: Vec<&str> = Vec::new();

    macro_rules! flush_paragraph {
        () => {{
            let text = current.join("\n");
            let text = text.trim_matches('\n');
            current.clear();
            // `if text.strip()`: whitespace-only accumulations vanish but
            // leave the pending key/title for the next block.
            if !py_strip(text).is_empty() {
                parsed.push(ParsedBlock {
                    key: pending_key.take(),
                    block_type: "paragraph".to_owned(),
                    title: pending_title.take(),
                    body: text.to_owned(),
                    level: 2,
                    parent_index: heading_stack.last().map(|(_, index)| *index),
                });
            }
        }};
    }

    while index < lines.len() {
        let line = lines[index];
        let stripped = py_strip(line);
        if let Some(captures) = BLOCK_COMMENT_RE.captures(stripped) {
            flush_paragraph!();
            let raw = captures.get(1).expect("block key group").as_str();
            pending_key = Some(Uuid::parse_str(raw).map_err(|_| {
                Error::Validation(format!("Bad block comment: {}", py_repr_str(stripped)))
            })?);
        } else if let Some(captures) = TITLE_COMMENT_RE.captures(stripped) {
            let title = captures.get(1).expect("title group").as_str();
            pending_title = if title.is_empty() {
                None
            } else {
                Some(title.to_owned())
            };
        } else if let Some(captures) = HEADING_RE.captures(line) {
            flush_paragraph!();
            let level = captures.get(1).expect("hashes").as_str().chars().count() as i64;
            let title = py_strip(captures.get(2).expect("heading text").as_str());
            let title = if title.is_empty() {
                None
            } else {
                Some(title.to_owned())
            };
            while heading_stack
                .last()
                .is_some_and(|(stacked, _)| *stacked >= level)
            {
                heading_stack.pop();
            }
            parsed.push(ParsedBlock {
                key: pending_key.take(),
                block_type: "heading".to_owned(),
                title,
                body: String::new(),
                level,
                parent_index: heading_stack.last().map(|(_, index)| *index),
            });
            heading_stack.push((level, parsed.len() - 1));
        } else if stripped.starts_with("```") {
            flush_paragraph!();
            let mut fence = vec![line];
            index += 1;
            while index < lines.len() && !py_strip(lines[index]).starts_with("```") {
                fence.push(lines[index]);
                index += 1;
            }
            // An unclosed fence runs to EOF: no closing line is appended.
            if index < lines.len() {
                fence.push(lines[index]);
            }
            parsed.push(ParsedBlock {
                key: pending_key.take(),
                block_type: "code".to_owned(),
                title: pending_title.take(),
                body: fence.join("\n"),
                level: 2,
                parent_index: heading_stack.last().map(|(_, index)| *index),
            });
        } else if stripped.starts_with('>') {
            flush_paragraph!();
            let mut quote = vec![line];
            index += 1;
            while index < lines.len() && py_strip(lines[index]).starts_with('>') {
                quote.push(lines[index]);
                index += 1;
            }
            index -= 1;
            parsed.push(ParsedBlock {
                key: pending_key.take(),
                block_type: "quotation".to_owned(),
                title: pending_title.take(),
                body: quote.join("\n"),
                level: 2,
                parent_index: heading_stack.last().map(|(_, index)| *index),
            });
        } else if LIST_ITEM_RE.is_match(line) && current.is_empty() {
            let mut items = vec![line];
            index += 1;
            while index < lines.len()
                && (LIST_ITEM_RE.is_match(lines[index])
                    || (!py_strip(lines[index]).is_empty()
                        && lines[index].chars().next().is_some_and(py_is_space)))
            {
                items.push(lines[index]);
                index += 1;
            }
            index -= 1;
            parsed.push(ParsedBlock {
                key: pending_key.take(),
                block_type: "list".to_owned(),
                title: pending_title.take(),
                body: items.join("\n"),
                level: 2,
                parent_index: heading_stack.last().map(|(_, index)| *index),
            });
        } else if FOOTNOTE_DEF_RE.is_match(stripped) {
            // Footnotes render from rows; definitions do not round-trip.
            // No flush: a definition never splits a paragraph.
        } else if stripped.is_empty() {
            flush_paragraph!();
        } else {
            current.push(line);
        }
        index += 1;
    }
    flush_paragraph!();
    Ok((Map::new(), parsed))
}

/// Citation keys with their block keys, and block depths, pre-copy.
///
/// `attached` pairs each citation key with its block row id; a key joins
/// the inventory only when its block is in the tree. Depths walk
/// `parent_id` links with a cycle guard, exactly like `_copy_inventory`.
pub fn copy_inventory(
    blocks: &[marginalia_types::works::WorkBlock],
    attached: &[(Uuid, Uuid)],
) -> (HashMap<String, String>, Vec<(String, i64)>) {
    let by_id: HashMap<Uuid, String> = blocks
        .iter()
        .map(|item| (item.id, item.block_key.to_string()))
        .collect();
    // Tree order is walked once into a Vec: the deletion pass stable-sorts
    // by depth descending, and Python's stable `sorted` keeps tree order on
    // ties via dict stability — a `HashMap` would erase that order and fall
    // back to key order. Lookups below stay exact; only iteration is ordered.
    let mut depth: Vec<(String, i64)> = Vec::with_capacity(blocks.len());
    for item in blocks {
        let mut level = 0;
        let mut parent = item.parent_id;
        let mut seen = HashMap::from([(item.id, ())]);
        while let Some(parent_id) = parent {
            if seen.contains_key(&parent_id) {
                break;
            }
            level += 1;
            seen.insert(parent_id, ());
            parent = blocks
                .iter()
                .find(|row| row.id == parent_id)
                .and_then(|row| row.parent_id);
        }
        depth.push((item.block_key.to_string(), level));
    }
    let known = attached
        .iter()
        .filter(|(_, block_id)| by_id.contains_key(block_id))
        .map(|(citation_key, block_id)| {
            (
                citation_key.to_string(),
                by_id.get(block_id).expect("filtered").clone(),
            )
        })
        .collect();
    (known, depth)
}

pub struct WorkExportService<W, R, B, C, L, S, Tx, F> {
    works: W,
    revisions: R,
    blocks: B,
    citations: C,
    links: L,
    spans: S,
    tx_factory: F,
    tx_marker: std::marker::PhantomData<Tx>,
}

impl<W, R, B, C, L, S, Tx, F> WorkExportService<W, R, B, C, L, S, Tx, F>
where
    W: WorkRepo<Tx = Tx>,
    R: WorkRevisionRepo<Tx = Tx>,
    B: WorkBlockRepo<Tx = Tx>,
    C: CitationRepo,
    L: WorkLinkRepo,
    S: SourceSpanRepo<Tx = Tx>,
    F: TxFactory<Tx = Tx>,
{
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        works: W,
        revisions: R,
        blocks: B,
        citations: C,
        links: L,
        spans: S,
        tx_factory: F,
    ) -> Self {
        Self {
            works,
            revisions,
            blocks,
            citations,
            links,
            spans,
            tx_factory,
            tx_marker: std::marker::PhantomData,
        }
    }

    /// Render the work's current revision to §6.4 markdown.
    pub async fn export_draft(&self, slug: Option<&str>, work_id: Option<Uuid>) -> Result<String> {
        let (work, revision) = self.resolve_current(slug, work_id).await?;
        let view = assemble_revision(
            &work,
            &revision,
            &self.blocks,
            &self.citations,
            &self.links,
            &self.spans,
        )
        .await?;
        Ok(render_markdown(&view))
    }

    /// Positional wrapper for the drift check's exporter callback.
    pub async fn export_draft_text(&self, work_id: Uuid) -> Result<String> {
        self.export_draft(None, Some(work_id)).await
    }

    /// Apply edited markdown as a new draft revision; refuse on dangling
    /// markers with nothing written.
    pub async fn import_draft(
        &self,
        slug: Option<&str>,
        work_id: Option<Uuid>,
        markdown: &str,
        dry_run: bool,
    ) -> ImportResult<ImportDiff> {
        let (work, revision) = self.resolve_current(slug, work_id).await?;
        let (front, parsed) = parse_markdown(markdown)?;
        if front
            .get("work")
            .is_none_or(|value| *value != Value::String(work.slug.clone()))
        {
            return Err(ImportError::Failed(Error::Validation(format!(
                "Import names work {}, not {}",
                py_repr_json(front.get("work")),
                py_repr_str(&work.slug)
            ))));
        }
        let (known_keys, depth_by_key) = self.copy_inventory_rows(revision.id).await?;
        let mut tx = self.tx_factory.begin().await?;
        let created = match self.revisions.copy_forward(&mut tx, revision.id).await {
            Ok(created) => created,
            Err(error) => {
                let _ = self.tx_factory.rollback(tx).await;
                return Err(ImportError::Failed(error));
            }
        };
        if let Err(refused) = check_markers_live(&parsed, &known_keys) {
            let _ = self.tx_factory.rollback(tx).await;
            return Err(ImportError::Refused(refused));
        }
        let changes = match self
            .apply_blocks(&mut tx, created.id, &parsed, &depth_by_key)
            .await
        {
            Ok(changes) => changes,
            Err(error) => {
                let _ = self.tx_factory.rollback(tx).await;
                return Err(ImportError::Failed(error));
            }
        };
        if dry_run {
            let _ = self.tx_factory.rollback(tx).await;
        } else {
            // The new draft is the work now: without this the import is
            // unreachable — get, validate, and freeze all default to
            // current, and the old draft would stay there.
            if let Err(error) = self
                .works
                .set_current_revision(&mut tx, work.id, created.id)
                .await
            {
                let _ = self.tx_factory.rollback(tx).await;
                return Err(ImportError::Failed(error));
            }
            self.tx_factory.commit(tx).await?;
        }
        Ok(ImportDiff {
            revision_id: created.id,
            revision_number: created.revision_number,
            dry_run,
            changes,
        })
    }

    pub async fn resolve_current(
        &self,
        slug: Option<&str>,
        work_id: Option<Uuid>,
    ) -> Result<(Work, WorkRevision)> {
        let work = if let Some(slug) = slug {
            self.works.get_by_slug(slug).await?
        } else if let Some(work_id) = work_id {
            self.works.get(work_id).await?
        } else {
            None
        };
        let Some(work) = work else {
            let id = slug
                .map(str::to_owned)
                .or_else(|| work_id.map(|id| id.to_string()))
                .unwrap_or_else(|| "None".to_owned());
            return Err(Error::NotFound { kind: "work", id });
        };
        let Some(current_id) = work.current_revision_id else {
            return Err(Error::NotFound {
                kind: "work_revision",
                id: format!("current of {}", work.slug),
            });
        };
        let revision = self.revisions.get(current_id).await?;
        let Some(revision) = revision else {
            return Err(Error::NotFound {
                kind: "work_revision",
                id: current_id.to_string(),
            });
        };
        Ok((work, revision))
    }

    /// `_copy_inventory`: the tree plus the revision's citation addresses,
    /// reduced through [`copy_inventory`].
    async fn copy_inventory_rows(
        &self,
        revision_id: Uuid,
    ) -> Result<(HashMap<String, String>, Vec<(String, i64)>)> {
        let tree = self.blocks.tree(revision_id).await?;
        let attached = self.citations.for_revision(revision_id).await?;
        let pairs: Vec<(Uuid, Uuid)> = attached
            .iter()
            .map(|entry| (entry.occurrence.citation_key, entry.occurrence.block_id))
            .collect();
        Ok(copy_inventory(&tree, &pairs))
    }

    /// `_apply`: inserts, updates, moves, and depth-ordered deletions.
    /// Marker validation ran up front in [`check_markers_live`]; only row
    /// writes remain, so this runs inside the import transaction.
    async fn apply_blocks(
        &self,
        tx: &mut Tx,
        revision_id: Uuid,
        parsed: &[ParsedBlock],
        depth_in_tree_order: &[(String, i64)],
    ) -> Result<Vec<BlockChange>> {
        let mut key_of_index: HashMap<usize, Uuid> = HashMap::new();
        let mut changes: Vec<BlockChange> = Vec::new();
        let mut sibling_position: HashMap<String, i64> = HashMap::new();
        for (index, block) in parsed.iter().enumerate() {
            // uuid_utils ids never cross into pydantic (see
            // WorkService.upsert_block); new keys are time-ordered v7s.
            let key = block.key.unwrap_or_else(Uuid::now_v7);
            let mut parent_key: Option<Uuid> = None;
            if let Some(parent_index) = block.parent_index {
                let Some(parent) = key_of_index.get(&parent_index) else {
                    return Err(Error::Validation(
                        "A block's parent must precede it in the file".to_owned(),
                    ));
                };
                parent_key = Some(*parent);
            }
            key_of_index.insert(index, key);
            let group = parent_key
                .map(|key| key.to_string())
                .unwrap_or_else(|| "None".to_owned());
            let position = sibling_position.get(&group).copied().unwrap_or(0);
            sibling_position.insert(group, position + 1);
            let mut parent_id = None;
            if let Some(parent_key) = parent_key {
                let parent_row = self
                    .blocks
                    .by_key_in_tx(tx, revision_id, parent_key)
                    .await?;
                let Some(parent_row) = parent_row else {
                    return Err(Error::Validation(format!(
                        "Parent block {parent_key} is not in this revision"
                    )));
                };
                parent_id = Some(parent_row.id);
            }
            let old = self.blocks.by_key_in_tx(tx, revision_id, key).await?;
            match old {
                None => {
                    let mut attributes = Map::new();
                    if block.block_type == "heading" {
                        attributes.insert("level".to_owned(), Value::from(block.level));
                    }
                    self.blocks
                        .upsert(
                            tx,
                            revision_id,
                            WorkBlockDraft {
                                revision_id,
                                block_key: key,
                                parent_id,
                                position,
                                block_type: block.block_type.clone(),
                                title: block.title.clone(),
                                body_markdown: block.body.clone(),
                                attributes,
                            },
                            None,
                        )
                        .await?;
                    changes.push(BlockChange {
                        block_key: key.to_string(),
                        change: "added".to_owned(),
                    });
                }
                Some(old) => {
                    let moved = old.parent_id != parent_id || old.position != position;
                    let edited = normalize_title(old.title.as_deref())
                        != normalize_title(block.title.as_deref())
                        || old.body_markdown != block.body
                        || old.block_type != block.block_type;
                    let mut attributes = old.attributes.clone();
                    let mut base = old.attributes.clone();
                    if block.block_type == "heading" {
                        // Export renders a missing level as 2; a row that
                        // never stored one is not a change on reimport.
                        attributes.insert("level".to_owned(), Value::from(block.level));
                        base.entry("level").or_insert(Value::from(2));
                    }
                    if moved || edited || attributes != base {
                        self.blocks
                            .upsert(
                                tx,
                                revision_id,
                                WorkBlockDraft {
                                    revision_id,
                                    block_key: key,
                                    parent_id,
                                    position,
                                    block_type: block.block_type.clone(),
                                    title: block.title.clone(),
                                    body_markdown: block.body.clone(),
                                    attributes,
                                },
                                Some(old.updated_at),
                            )
                            .await?;
                        changes.push(BlockChange {
                            block_key: key.to_string(),
                            change: if moved { "moved" } else { "updated" }.to_owned(),
                        });
                    }
                }
            }
        }
        let imported_keys: HashMap<String, ()> = key_of_index
            .values()
            .map(|key| (key.to_string(), ()))
            .collect();
        // Deepest first so children vanish before their parents. The sort is
        // stable over tree order, exactly like Python's stable `sorted` over
        // its insertion-ordered depth dict — ties keep tree order.
        let depth_of = |key: &str| {
            depth_in_tree_order
                .iter()
                .find(|(candidate, _)| candidate == key)
                .map(|(_, level)| *level)
                .unwrap_or(0)
        };
        let mut removed: Vec<&String> = depth_in_tree_order
            .iter()
            .map(|(key, _)| key)
            .filter(|key| !imported_keys.contains_key(*key))
            .collect();
        removed.sort_by_key(|left| std::cmp::Reverse(depth_of(left)));
        for key in removed {
            let parsed_key = Uuid::parse_str(key).expect("depth keys are block keys");
            if let Some(row) = self
                .blocks
                .by_key_in_tx(tx, revision_id, parsed_key)
                .await?
            {
                self.blocks.delete(tx, row.id).await?;
                changes.push(BlockChange {
                    block_key: key.clone(),
                    change: "deleted".to_owned(),
                });
            }
        }
        Ok(changes)
    }
}

/// `old.title or None`: empty titles compare as missing on both sides.
fn normalize_title(title: Option<&str>) -> Option<&str> {
    title.filter(|text| !text.is_empty())
}

/// `f"{front.get('work')!r}"`: Python `repr` over the loaded YAML scalar.
fn py_repr_json(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".to_owned(),
        Some(Value::Bool(true)) => "True".to_owned(),
        Some(Value::Bool(false)) => "False".to_owned(),
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::String(text)) => py_repr_str(text),
        Some(Value::Array(items)) => {
            let inner: Vec<String> = items.iter().map(|item| py_repr_json(Some(item))).collect();
            format!("[{}]", inner.join(", "))
        }
        Some(Value::Object(map)) => {
            let inner: Vec<String> = map
                .iter()
                .map(|(key, item)| format!("{}: {}", py_repr_str(key), py_repr_json(Some(item))))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
    }
}

/// The per-block apply decisions that do not need a transaction: marker
/// validation against the live-key inventory. Pure; the row writes stay in
/// `import_draft`.
pub fn check_markers_live(
    parsed: &[ParsedBlock],
    known_keys: &HashMap<String, String>,
) -> std::result::Result<HashMap<String, String>, ImportRefused> {
    // Blocks without a comment are new rows, so only explicitly keyed
    // blocks survive: markers naming occurrences of deleted blocks are
    // dangling, decided here before any row moves.
    let kept: HashMap<String, ()> = parsed
        .iter()
        .filter_map(|block| block.key.map(|key| (key.to_string(), ())))
        .collect();
    let live_keys: HashMap<String, String> = known_keys
        .iter()
        .filter(|(_, holder)| kept.contains_key(*holder))
        .map(|(cited, holder)| (cited.clone(), holder.clone()))
        .collect();
    for block in parsed {
        let (keys, invalid) = find_markers(&block.body);
        if let Some(raw) = invalid.first() {
            return Err(ImportRefused {
                rule_id: "AUTH_CITATION_MARKER_DANGLING".to_owned(),
                message: format!("Marker {raw} names no citation: keys are UUIDs"),
                detail: Some(Map::from_iter([(
                    "marker".to_owned(),
                    Value::String(raw.clone()),
                )])),
            });
        }
        for key in keys {
            if !live_keys.contains_key(&key) {
                return Err(ImportRefused {
                    rule_id: "AUTH_CITATION_MARKER_DANGLING".to_owned(),
                    message: format!("Marker {{{{cite:{key}}}}} matches no occurrence"),
                    detail: Some(Map::from_iter([(
                        "citation_key".to_owned(),
                        Value::String(key.clone()),
                    )])),
                });
            }
        }
    }
    Ok(live_keys)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::future::Future;
    use std::pin::Pin;
    use std::sync::Mutex;
    use std::task::{Context, Poll, RawWaker, RawWakerVTable, Waker};

    use chrono::Utc;
    use marginalia_types::citations::{BlockCitations, CitationItem, CitationOccurrence};
    use marginalia_types::spans::SourceSpan;
    use marginalia_types::works::{BlockLinks, WorkBlock, WorkStatus};

    use crate::assembly::AssembledBlock;

    /// Minimal executor: the fakes never pend, so a spin poll drives the
    /// future to readiness. (`tokio` is not a dependency of this crate,
    /// and `Cargo.toml` is owned by another slice.)
    fn block_on<F: Future>(mut future: F) -> F::Output {
        fn raw() -> RawWaker {
            fn noop(_: *const ()) {}
            fn clone(ptr: *const ()) -> RawWaker {
                raw_waker(ptr)
            }
            fn raw_waker(ptr: *const ()) -> RawWaker {
                RawWaker::new(ptr, &RawWakerVTable::new(clone, noop, noop, noop))
            }
            raw_waker(std::ptr::null())
        }
        // SAFETY: the waker never dereferences its null data pointer; the
        // futures polled here never clone, wake, or drop through it.
        let waker = unsafe { Waker::from_raw(raw()) };
        let mut context = Context::from_waker(&waker);
        // SAFETY: `future` is stack-owned and never moved after pinning.
        let mut pinned = unsafe { Pin::new_unchecked(&mut future) };
        loop {
            match pinned.as_mut().poll(&mut context) {
                Poll::Ready(value) => return value,
                Poll::Pending => std::thread::yield_now(),
            }
        }
    }

    #[test]
    // The clone-then-wake is the whole point: it fires the vtable `clone`
    // callback, which no production path in these tests otherwise reaches.
    #[allow(clippy::waker_clone_wake)]
    fn test_block_on_waker_clone_fires_vtable_clone() {
        // The vtable `clone` callback only fires when a polled future clones
        // its waker; the first poll also drives `block_on`'s `Pending` arm
        // (null data pointer survives clone, and the loop re-polls).
        struct Cloner(bool);
        impl Future for Cloner {
            type Output = ();
            fn poll(
                mut self: std::pin::Pin<&mut Self>,
                context: &mut std::task::Context<'_>,
            ) -> std::task::Poll<()> {
                if !self.0 {
                    self.0 = true;
                    context.waker().clone().wake();
                    return std::task::Poll::Pending;
                }
                std::task::Poll::Ready(())
            }
        }
        block_on(Cloner(false));
    }

    const KEY_A: &str = "11111111-1111-1111-1111-111111111111";
    const KEY_B: &str = "22222222-2222-2222-2222-222222222222";
    const CITE_A: &str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    const WORK_ID: &str = "33333333-3333-3333-3333-333333333333";
    const REV_ID: &str = "44444444-4444-4444-4444-444444444444";
    const ROW_A: &str = "55555555-5555-5555-5555-555555555555";
    const ROW_B: &str = "66666666-6666-6666-6666-666666666666";
    const DOC_ID: &str = "cccccccc-cccc-cccc-cccc-cccccccccccc";

    fn uuid(raw: &str) -> Uuid {
        Uuid::parse_str(raw).expect("fixture uuid")
    }

    fn work(title: &str) -> Work {
        let now = Utc::now();
        Work {
            id: uuid(WORK_ID),
            slug: "deror".to_owned(),
            title: title.to_owned(),
            work_type: "essay".to_owned(),
            status: WorkStatus::Draft,
            language: None,
            abstract_text: None,
            current_revision_id: Some(uuid(REV_ID)),
            metadata: Map::new(),
            created_at: now,
            updated_at: now,
            archived_at: None,
        }
    }

    fn revision(number: i64) -> WorkRevision {
        let now = Utc::now();
        WorkRevision {
            id: uuid(REV_ID),
            work_id: uuid(WORK_ID),
            revision_number: number,
            parent_revision_id: None,
            state: RevisionState::Draft,
            message: None,
            content_hash: None,
            created_by: "user".to_owned(),
            created_at: now,
            frozen_at: None,
            published_at: None,
            metadata: Map::new(),
        }
    }

    /// Fields for the `row` test helper, grouped so the constructor takes
    /// one parameter instead of eight positional arguments.
    struct RowParams<'a> {
        id: &'a str,
        key: &'a str,
        parent_id: Option<Uuid>,
        position: i64,
        block_type: &'a str,
        title: Option<&'a str>,
        body: &'a str,
        attributes: Map<String, Value>,
    }

    fn row(params: RowParams<'_>) -> WorkBlock {
        let now = Utc::now();
        WorkBlock {
            id: uuid(params.id),
            revision_id: uuid(REV_ID),
            block_key: uuid(params.key),
            parent_id: params.parent_id,
            position: params.position,
            block_type: params.block_type.to_owned(),
            title: params.title.map(str::to_owned),
            body_markdown: params.body.to_owned(),
            attributes: params.attributes,
            created_at: now,
            updated_at: now,
        }
    }

    fn level_attributes(level: i64) -> Map<String, Value> {
        let mut attributes = Map::new();
        attributes.insert("level".to_owned(), Value::from(level));
        attributes
    }

    fn occurrence(block_id: Uuid) -> CitationOccurrence {
        CitationOccurrence {
            id: uuid("77777777-7777-7777-7777-777777777777"),
            citation_key: uuid(CITE_A),
            block_id,
            placement: Placement::BlockEnd,
            intent: Intent::Background,
            note: None,
            created_at: Utc::now(),
        }
    }

    fn item(occurrence_id: Uuid) -> CitationItem {
        CitationItem {
            occurrence_id,
            position: 0,
            edition_id: None,
            edition_key: Some("DABAR_2026".to_owned()),
            source_span_id: None,
            quoted_text: None,
            verify_status: None,
            verified_at: None,
            locator: Map::new(),
            prefix: None,
            suffix: None,
            suppress_author: false,
        }
    }

    /// `test_export_renders_comments_citations_and_block_end_markers`: the
    /// expected bytes are CPython `render_markdown` output for the same
    /// fixture, captured byte for byte.
    fn cited_view(work_title: &str) -> AssembledRevision {
        let heading = row(RowParams {
            id: ROW_A,
            key: KEY_A,
            parent_id: None,
            position: 0,
            block_type: "heading",
            title: Some("Release"),
            body: "",
            attributes: level_attributes(2),
        });
        let paragraph = row(RowParams {
            id: ROW_B,
            key: KEY_B,
            parent_id: Some(uuid(ROW_A)),
            position: 0,
            block_type: "paragraph",
            title: None,
            body: "A background claim.",
            attributes: Map::new(),
        });
        let occ = occurrence(uuid(ROW_B));
        let entry = BlockCitations {
            occurrence: occ.clone(),
            items: vec![item(occ.id)],
        };
        AssembledRevision {
            work: work(work_title),
            revision: revision(1),
            blocks: vec![
                AssembledBlock {
                    block: heading,
                    parent_key: None,
                    citations: Vec::new(),
                    links: Some(BlockLinks::default()),
                },
                AssembledBlock {
                    block: paragraph,
                    parent_key: Some(uuid(KEY_A)),
                    citations: vec![entry],
                    links: Some(BlockLinks::default()),
                },
            ],
            spans: HashMap::new(),
        }
    }

    #[test]
    fn test_export_renders_comments_citations_and_block_end_markers() {
        let rendered = render_markdown(&cited_view("Release: Über"));
        assert_eq!(
            rendered,
            "---\n\
             work: deror\n\
             title: 'Release: Über'\n\
             type: essay\n\
             revision: 1\n\
             state: draft\n\
             citations:\n\
             - key: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n\
             \x20 intent: background\n\
             \x20 edition_key: DABAR_2026\n\
             ---\n\
             \n\
             <!-- block:11111111-1111-1111-1111-111111111111 -->\n\
             ## Release\n\
             \n\
             <!-- block:22222222-2222-2222-2222-222222222222 -->\n\
             A background claim.\n\
             {{cite:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}}\n"
        );
        assert!(rendered.contains("<!-- block:11111111-1111-1111-1111-111111111111 -->"));
        assert!(rendered.contains("## Release"));
        assert!(rendered.contains("DABAR_2026"));
        assert!(rendered.contains(&format_marker(&uuid(CITE_A))));
    }

    /// `test_parse_recovers_keys_types_titles_and_parents`.
    #[test]
    fn test_parse_recovers_keys_types_titles_and_parents() {
        let (front, parsed) =
            parse_markdown(&render_markdown(&cited_view("Deror"))).expect("parse");
        assert_eq!(front.get("work"), Some(&Value::String("deror".to_owned())));
        let keys: Vec<Option<Uuid>> = parsed.iter().map(|block| block.key).collect();
        assert_eq!(keys, vec![Some(uuid(KEY_A)), Some(uuid(KEY_B))]);
        let types: Vec<&str> = parsed
            .iter()
            .map(|block| block.block_type.as_str())
            .collect();
        assert_eq!(types, vec!["heading", "paragraph"]);
        assert_eq!(parsed[0].title.as_deref(), Some("Release"));
        assert_eq!(parsed[1].parent_index, Some(0));
        assert!(parsed[1].body.contains(&format_marker(&uuid(CITE_A))));
    }

    /// `test_parse_drops_footnote_definitions`.
    #[test]
    fn test_parse_drops_footnote_definitions() {
        let markdown = "---\nwork: deror\n---\n\n<!-- block:11111111-1111-1111-1111-111111111111 -->\nText.\n\n[^c1]: a definition\n";
        let (_, parsed) = parse_markdown(markdown).expect("parse");
        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed[0].body, "Text.");
    }

    /// `test_import_without_front_matter_is_invalid`.
    #[test]
    fn test_import_without_front_matter_is_invalid() {
        let error = parse_markdown("No front matter here.\n").expect_err("must fail");
        assert!(
            matches!(error, Error::Validation(message) if message == "Import needs YAML front matter between --- lines")
        );
    }

    /// Width-80 plain wrapping with the two-space continuation, captured
    /// from CPython for the same long title.
    #[test]
    fn test_yaml_long_title_wraps_at_width() {
        let view = cited_view(
            "A very long title that goes on and on and on and on and on and on and on and on",
        );
        let rendered = render_markdown(&view);
        let front = rendered
            .split("---\n")
            .nth(1)
            .expect("front matter")
            .to_owned();
        assert!(front.starts_with(
            "work: deror\ntitle: A very long title that goes on and on and on and on and on and on and on and\n  on\ntype: essay\n"
        ));
    }

    /// Embedded newlines single-quote with one blank line per `\n` and a
    /// four-space continuation at this nesting depth, per CPython.
    #[test]
    fn test_yaml_quoted_text_newlines_single_quoted() {
        let mut view = cited_view("T");
        let occ = occurrence(uuid(ROW_B));
        let mut quoted = item(occ.id);
        quoted.source_span_id = Some(uuid("99999999-9999-9999-9999-999999999999"));
        quoted.quoted_text = Some("line1\nline2 \"q\" done".to_owned());
        view.blocks[1].citations = vec![BlockCitations {
            occurrence: occ,
            items: vec![quoted],
        }];
        view.spans.insert(
            uuid("99999999-9999-9999-9999-999999999999"),
            SourceSpan {
                id: uuid("99999999-9999-9999-9999-999999999999"),
                document_id: uuid(DOC_ID),
                char_start: 5,
                char_end: 12,
                quoted_text: "canon".to_owned(),
                parser: None,
                parser_version: None,
                passage_id: None,
                created_at: Utc::now(),
            },
        );
        let rendered = render_markdown(&view);
        assert!(rendered.contains("quoted_text: 'line1\n\n    line2 \"q\" done'\n"));
    }

    /// Locator value shapes (string needing quotes, int, bool, null) with
    /// CPython key order and indentation, captured from CPython.
    #[test]
    fn test_yaml_locator_shapes() {
        let mut view = cited_view("T");
        let occ = occurrence(uuid(ROW_B));
        let mut located = item(occ.id);
        located.locator = Map::from_iter([
            ("page".to_owned(), Value::String("12".to_owned())),
            ("vol".to_owned(), Value::from(3)),
            ("flag".to_owned(), Value::Bool(true)),
            ("nothing".to_owned(), Value::Null),
        ]);
        view.blocks[1].citations = vec![BlockCitations {
            occurrence: occ,
            items: vec![located],
        }];
        let rendered = render_markdown(&view);
        // Insertion order rides through, like CPython's `dict(row.locator)`:
        // page, vol, flag, nothing as constructed above, not sorted.
        assert!(rendered.contains(
            "  locator:\n    page: '12'\n    vol: 3\n    flag: true\n    nothing: null\n"
        ));
    }

    #[test]
    fn test_yaml_locator_false_value_and_boolish_key() {
        // `false` rides the `Bool(false)` emitter arm (not just `true`),
        // and a key that reads as a bool literal still renders single-quoted
        // through the simple-key style path.
        let mut view = cited_view("T");
        let occ = occurrence(uuid(ROW_B));
        let mut located = item(occ.id);
        located.locator = Map::from_iter([
            ("neg".to_owned(), Value::Bool(false)),
            ("true".to_owned(), Value::from(1)),
        ]);
        view.blocks[1].citations = vec![BlockCitations {
            occurrence: occ,
            items: vec![located],
        }];
        let rendered = render_markdown(&view);
        assert!(rendered.contains("    neg: false\n"), "{rendered}");
        assert!(rendered.contains("    'true': 1\n"), "{rendered}");
    }

    #[test]
    fn test_render_omits_a_missing_edition_key() {
        // A citation item with no edition rides the `None` arm: the row
        // renders without an `edition_key` line, everything else intact.
        let mut view = cited_view("T");
        let occ = occurrence(uuid(ROW_B));
        let mut keyless = item(occ.id);
        keyless.edition_key = None;
        view.blocks[1].citations = vec![BlockCitations {
            occurrence: occ,
            items: vec![keyless],
        }];
        let rendered = render_markdown(&view);
        assert!(!rendered.contains("edition_key"), "{rendered}");
        assert!(rendered.contains("intent: background\n"), "{rendered}");
    }

    /// Implicit-resolution quoting over the exporter scalar shapes: each
    /// title rides the same `write_scalar` path as every front string.
    #[test]
    fn test_yaml_implicit_resolution_quoting() {
        let cases = [
            ("Release", "title: Release\n"),
            ("it's \"quoted\"", "title: it's \"quoted\"\n"),
            ("héllo wörld", "title: héllo wörld\n"),
            ("Release: A Study", "title: 'Release: A Study'\n"),
            ("true", "title: 'true'\n"),
            ("yes", "title: 'yes'\n"),
            ("on", "title: 'on'\n"),
            ("123", "title: '123'\n"),
            ("3.14", "title: '3.14'\n"),
            ("2024-01-02", "title: '2024-01-02'\n"),
            ("null", "title: 'null'\n"),
            ("~", "title: '~'\n"),
            ("-", "title: '-'\n"),
            ("- item", "title: '- item'\n"),
            ("a # b", "title: 'a # b'\n"),
            ("trailing ", "title: 'trailing '\n"),
            ("  indented", "title: '  indented'\n"),
            ("", "title: ''\n"),
            ("08", "title: 08\n"),
            ("1e5", "title: 1e5\n"),
            ("n", "title: n\n"),
        ];
        for (title, expected) in cases {
            let rendered = render_markdown(&cited_view(title));
            let line = rendered
                .lines()
                .find(|line| line.starts_with("title:"))
                .expect("title line");
            assert_eq!(line.to_owned() + "\n", expected, "title {title:?}");
        }
    }

    /// Heading `int(level)` coercions (bool/float/string/missing) with the
    /// 1..=6 clamp, mirroring CPython's `int()`.
    #[test]
    fn test_heading_level_coercions() {
        let levels = [
            (None, "## Missing\n"),
            (Some(Value::Bool(true)), "# True\n"),
            (Some(Value::Bool(false)), "# False\n"),
            (Some(Value::from(4)), "#### Four\n"),
            (Some(Value::from(2.9)), "## Float\n"),
            (Some(Value::String("3".to_owned())), "### String\n"),
            (Some(Value::String("  5  ".to_owned())), "##### Padded\n"),
            (Some(Value::from(0)), "# Low\n"),
            (Some(Value::from(9)), "###### High\n"),
        ];
        for (level, expected) in levels {
            let mut attributes = Map::new();
            if let Some(level) = level {
                attributes.insert("level".to_owned(), level);
            }
            let heading = row(RowParams {
                id: ROW_A,
                key: KEY_A,
                parent_id: None,
                position: 0,
                block_type: "heading",
                title: Some("Titled"),
                body: "",
                attributes,
            });
            let title = expected
                .trim_end_matches('\n')
                .split_once(' ')
                .expect("line")
                .1;
            let heading = WorkBlock {
                title: Some(title.to_owned()),
                ..heading
            };
            let view = AssembledRevision {
                work: work("T"),
                revision: revision(1),
                blocks: vec![AssembledBlock {
                    block: heading,
                    parent_key: None,
                    citations: Vec::new(),
                    links: Some(BlockLinks::default()),
                }],
                spans: HashMap::new(),
            };
            let rendered = render_markdown(&view);
            assert!(
                rendered.contains(expected),
                "level case {expected:?}:\n{rendered}"
            );
        }
    }

    #[test]
    fn test_heading_level_rejects_a_non_integer() {
        // `int()` coercion failure surfaces as a validation error with
        // CPython's message, not a clamp.
        let mut attributes = Map::new();
        attributes.insert("level".to_owned(), Value::String("high".to_owned()));
        let error = heading_level(&attributes).expect_err("non-integer level");
        assert_eq!(
            format!("{error}"),
            "data validation failed: invalid literal for int() with base 10: 'high'"
        );
    }

    #[test]
    fn test_parse_code_fences_closed_and_unclosed() {
        let markdown = "---\nwork: deror\n---\n\n<!-- block:11111111-1111-1111-1111-111111111111 -->\n```python\nprint(1)\n```\n\n<!-- block:22222222-2222-2222-2222-222222222222 -->\n```\nunclosed\n";
        let (_, parsed) = parse_markdown(markdown).expect("parse");
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].block_type, "code");
        assert_eq!(parsed[0].body, "```python\nprint(1)\n```");
        assert_eq!(parsed[1].block_type, "code");
        // An unclosed fence runs to EOF with no closing line appended.
        assert_eq!(parsed[1].body, "```\nunclosed");
    }

    #[test]
    fn test_parse_quotes_lists_titles_and_heading_parents() {
        let markdown = "---\nwork: deror\n---\n\n# Top\n\n<!-- title: Aside -->\nA note.\n\n> First\n> Second\n\n- one\n- two\n  continued\n\n## Child\n\nTail.\n";
        let (_, parsed) = parse_markdown(markdown).expect("parse");
        let types: Vec<&str> = parsed
            .iter()
            .map(|block| block.block_type.as_str())
            .collect();
        assert_eq!(
            types,
            vec![
                "heading",
                "paragraph",
                "quotation",
                "list",
                "heading",
                "paragraph"
            ]
        );
        assert_eq!(parsed[0].title.as_deref(), Some("Top"));
        assert_eq!(parsed[1].title.as_deref(), Some("Aside"));
        assert_eq!(parsed[1].body, "A note.");
        assert_eq!(parsed[2].body, "> First\n> Second");
        assert_eq!(parsed[3].body, "- one\n- two\n  continued");
        assert_eq!(parsed[4].parent_index, Some(0));
        assert_eq!(parsed[5].parent_index, Some(4));
    }

    #[test]
    fn test_parse_bad_block_comment() {
        // 36 hex digits with no hyphens match the comment pattern but fail
        // `UUID()`, exactly like the Python probe.
        let error = parse_markdown(
            "---\nwork: deror\n---\n\n<!-- block:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->\nText.\n",
        )
        .expect_err("must fail");
        assert!(
            matches!(error, Error::Validation(message) if message == "Bad block comment: '<!-- block:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->'")
        );
    }

    #[test]
    fn test_copy_inventory_depths_cycle_and_orphans() {
        let grandchild = row(RowParams {
            id: "aaaaaaaa-aaaa-aaaa-aaaa-000000000001",
            key: "aaaaaaaa-aaaa-aaaa-aaaa-000000000011",
            parent_id: Some(uuid("aaaaaaaa-aaaa-aaaa-aaaa-000000000002")),
            position: 0,
            block_type: "paragraph",
            title: None,
            body: "leaf",
            attributes: Map::new(),
        });
        let child = WorkBlock {
            id: uuid("aaaaaaaa-aaaa-aaaa-aaaa-000000000002"),
            // A cycle back to the leaf: the walk stops instead of looping.
            parent_id: Some(grandchild.id),
            ..row(RowParams {
                id: "aaaaaaaa-aaaa-aaaa-aaaa-000000000002",
                key: "aaaaaaaa-aaaa-aaaa-aaaa-000000000022",
                parent_id: None,
                position: 0,
                block_type: "paragraph",
                title: None,
                body: "middle",
                attributes: Map::new(),
            })
        };
        let root = row(RowParams {
            id: "aaaaaaaa-aaaa-aaaa-aaaa-000000000003",
            key: "aaaaaaaa-aaaa-aaaa-aaaa-000000000033",
            parent_id: None,
            position: 0,
            block_type: "paragraph",
            title: None,
            body: "root",
            attributes: Map::new(),
        });
        let blocks = vec![grandchild.clone(), child.clone(), root.clone()];
        let attached = vec![
            (uuid(CITE_A), grandchild.id),
            (uuid("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"), uuid(ROW_A)),
        ];
        let (known, depth) = copy_inventory(&blocks, &attached);
        assert_eq!(
            known,
            HashMap::from([(
                CITE_A.to_owned(),
                "aaaaaaaa-aaaa-aaaa-aaaa-000000000011".to_owned()
            )])
        );
        let depth_of = |key: &str| {
            depth
                .iter()
                .find(|(candidate, _)| candidate == key)
                .map(|(_, level)| *level)
        };
        assert_eq!(
            depth_of("aaaaaaaa-aaaa-aaaa-aaaa-000000000011"),
            Some(1),
            "the walk stops at the repeated id, like the Python walk"
        );
        assert_eq!(depth_of("aaaaaaaa-aaaa-aaaa-aaaa-000000000033"), Some(0));
    }
    #[test]
    fn test_check_markers_live_invalid_and_unknown() {
        let live = HashMap::from([(CITE_A.to_owned(), KEY_B.to_owned())]);
        let invalid = vec![ParsedBlock {
            key: Some(uuid(KEY_B)),
            block_type: "paragraph".to_owned(),
            title: None,
            body: "Ghost {{cite:c1}}.".to_owned(),
            level: 2,
            parent_index: None,
        }];
        let refused = check_markers_live(&invalid, &live).expect_err("invalid");
        assert_eq!(refused.rule_id, "AUTH_CITATION_MARKER_DANGLING");
        assert_eq!(
            refused.message,
            "Marker {{cite:c1}} names no citation: keys are UUIDs"
        );
        assert_eq!(
            refused.detail,
            Some(Map::from_iter([(
                "marker".to_owned(),
                Value::String("{{cite:c1}}".to_owned())
            )]))
        );
        let unknown = vec![ParsedBlock {
            key: Some(uuid(KEY_B)),
            block_type: "paragraph".to_owned(),
            title: None,
            body: "Ghost {{cite:99999999-9999-9999-9999-999999999999}}.".to_owned(),
            level: 2,
            parent_index: None,
        }];
        let refused = check_markers_live(&unknown, &live).expect_err("unknown");
        assert_eq!(refused.rule_id, "AUTH_CITATION_MARKER_DANGLING");
        assert_eq!(
            refused.message,
            "Marker {{cite:99999999-9999-9999-9999-999999999999}} matches no occurrence"
        );
        assert_eq!(
            refused.detail,
            Some(Map::from_iter([(
                "citation_key".to_owned(),
                Value::String("99999999-9999-9999-9999-999999999999".to_owned())
            )]))
        );
    }
    // --- Import service fakes -------------------------------------------

    #[derive(Clone, Copy)]
    struct Shared<'a>(&'a Mutex<Fakes>);
    struct FakeTx;

    struct Fakes {
        work: Option<Work>,
        revision: Option<WorkRevision>,
        created: WorkRevision,
        tree: Vec<WorkBlock>,
        attached: Vec<BlockCitations>,
        rows: HashMap<String, WorkBlock>,
        upserts: Vec<(WorkBlockDraft, Option<chrono::DateTime<Utc>>)>,
        deletes: Vec<Uuid>,
        set_current: Vec<(Uuid, Uuid)>,
        tx_events: Vec<&'static str>,
        works: HashMap<Uuid, Work>,
        revisions: HashMap<Uuid, WorkRevision>,
        occurrences: Vec<CitationOccurrence>,
        items: Vec<CitationItem>,
        source_links: Vec<marginalia_types::works::BlockSourceLink>,
        entity_links: Vec<marginalia_types::works::BlockEntityLink>,
        spans: HashMap<Uuid, SourceSpan>,
        fail_copy_forward: bool,
        fail_upsert: bool,
        fail_set_current: bool,
        fail_begin: bool,
        fail_commit: bool,
        fail_get_by_slug: bool,
        fail_get_work: bool,
        fail_rev_get: bool,
        fail_tree: bool,
        fail_for_revision: bool,
        fail_by_key_on_call: Option<u64>,
        by_key_calls: u64,
        fail_delete: bool,
        hidden_keys: Vec<Uuid>,
    }

    impl Fakes {
        /// A work with a heading and a cited paragraph, mirroring the
        /// `spine-loop`/`spine-dangle` integration setups.
        fn rig() -> Self {
            let heading = row(RowParams {
                id: ROW_A,
                key: KEY_A,
                parent_id: None,
                position: 0,
                block_type: "heading",
                title: Some("Release"),
                body: "",
                attributes: level_attributes(2),
            });
            let paragraph = row(RowParams {
                id: ROW_B,
                key: KEY_B,
                parent_id: Some(uuid(ROW_A)),
                position: 0,
                block_type: "paragraph",
                title: None,
                body: "The prophets speak. {{cite:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}}.",
                attributes: Map::new(),
            });
            let occ = occurrence(uuid(ROW_B));
            let current = revision(1);
            let mut created = revision(2);
            created.id = uuid("99999999-0000-0000-0000-000000000000");
            Self {
                work: Some(work("Deror")),
                revision: Some(current),
                created,
                tree: vec![heading, paragraph],
                attached: vec![BlockCitations {
                    occurrence: occ.clone(),
                    items: vec![item(occ.id)],
                }],
                rows: HashMap::new(),
                upserts: Vec::new(),
                deletes: Vec::new(),
                set_current: Vec::new(),
                tx_events: Vec::new(),
                works: HashMap::new(),
                revisions: HashMap::new(),
                occurrences: Vec::new(),
                items: Vec::new(),
                source_links: Vec::new(),
                entity_links: Vec::new(),
                spans: HashMap::new(),
                fail_copy_forward: false,
                fail_upsert: false,
                fail_set_current: false,
                fail_begin: false,
                fail_commit: false,
                fail_get_by_slug: false,
                fail_get_work: false,
                fail_rev_get: false,
                fail_tree: false,
                fail_for_revision: false,
                fail_by_key_on_call: None,
                by_key_calls: 0,
                fail_delete: false,
                hidden_keys: Vec::new(),
            }
        }

        fn seed_rows(&mut self) {
            // `copy_forward` carries every row into the new revision.
            for block in &self.tree {
                let mut carried = block.clone();
                carried.revision_id = self.created.id;
                self.rows.insert(block.block_key.to_string(), carried);
            }
        }

        fn markdown(&self) -> String {
            let heading = self.tree[0].clone();
            let paragraph = self.tree[1].clone();
            let view = AssembledRevision {
                work: self.work.clone().expect("work"),
                revision: self.revision.clone().expect("revision"),
                blocks: vec![
                    AssembledBlock {
                        block: heading,
                        parent_key: None,
                        citations: Vec::new(),
                        links: Some(BlockLinks::default()),
                    },
                    AssembledBlock {
                        block: paragraph,
                        parent_key: Some(uuid(KEY_A)),
                        citations: self.attached.clone(),
                        links: Some(BlockLinks::default()),
                    },
                ],
                spans: HashMap::new(),
            };
            render_markdown(&view)
        }
    }

    impl<'a> TxFactory for Shared<'a> {
        type Tx = FakeTx;
        async fn begin(&self) -> Result<FakeTx> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_begin {
                return Err(Error::Storage("begin injected".to_owned()));
            }
            guard.tx_events.push("begin");
            Ok(FakeTx)
        }
        async fn commit(&self, _tx: FakeTx) -> Result<()> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_commit {
                return Err(Error::Storage("commit injected".to_owned()));
            }
            guard.tx_events.push("commit");
            Ok(())
        }
        async fn rollback(&self, _tx: FakeTx) -> Result<()> {
            self.0.lock().expect("lock").tx_events.push("rollback");
            Ok(())
        }
    }

    impl<'a> WorkRepo for Shared<'a> {
        type Tx = FakeTx;
        async fn insert(
            &self,
            _tx: &mut FakeTx,
            draft: marginalia_types::works::WorkDraft,
        ) -> Result<Work> {
            let mut guard = self.0.lock().expect("lock");
            let now = Utc::now();
            let work = Work {
                id: Uuid::new_v4(),
                slug: draft.slug,
                title: draft.title,
                work_type: draft.work_type,
                status: WorkStatus::Draft,
                language: draft.language,
                abstract_text: draft.abstract_text,
                current_revision_id: None,
                metadata: draft.metadata,
                created_at: now,
                updated_at: now,
                archived_at: None,
            };
            guard.works.insert(work.id, work.clone());
            Ok(work)
        }
        async fn get(&self, work_id: Uuid) -> Result<Option<Work>> {
            let guard = self.0.lock().expect("lock");
            if guard.fail_get_work {
                return Err(Error::Storage("get_work injected".to_owned()));
            }
            if let Some(work) = guard.work.clone().filter(|work| work.id == work_id) {
                return Ok(Some(work));
            }
            Ok(guard.works.get(&work_id).cloned())
        }
        async fn get_by_slug(&self, slug: &str) -> Result<Option<Work>> {
            let guard = self.0.lock().expect("lock");
            if guard.fail_get_by_slug {
                return Err(Error::Storage("get_by_slug injected".to_owned()));
            }
            if let Some(work) = guard.work.clone().filter(|work| work.slug == slug) {
                return Ok(Some(work));
            }
            Ok(guard.works.values().find(|work| work.slug == slug).cloned())
        }
        async fn list(&self) -> Result<Vec<Work>> {
            let guard = self.0.lock().expect("lock");
            let mut works: Vec<Work> = guard.work.clone().into_iter().collect();
            works.extend(
                guard
                    .works
                    .values()
                    .filter(|extra| guard.work.as_ref().is_none_or(|work| work.id != extra.id))
                    .cloned(),
            );
            Ok(works)
        }
        async fn set_current_revision(
            &self,
            _tx: &mut FakeTx,
            work_id: Uuid,
            revision_id: Uuid,
        ) -> Result<()> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_set_current {
                return Err(Error::Storage("set_current injected".to_owned()));
            }
            if let Some(work) = guard.work.as_mut().filter(|work| work.id == work_id) {
                work.current_revision_id = Some(revision_id);
            }
            if let Some(work) = guard.works.get_mut(&work_id) {
                work.current_revision_id = Some(revision_id);
            }
            guard.set_current.push((work_id, revision_id));
            Ok(())
        }
        async fn update(
            &self,
            _tx: &mut FakeTx,
            work_id: Uuid,
            _expected_updated_at: chrono::DateTime<Utc>,
            fields: Map<String, Value>,
        ) -> Result<Work> {
            let mut guard = self.0.lock().expect("lock");
            let now = Utc::now();
            let target = if guard.work.as_ref().is_some_and(|work| work.id == work_id) {
                guard.work.as_mut()
            } else {
                guard.works.get_mut(&work_id)
            };
            let Some(work) = target else {
                return Err(Error::NotFound {
                    kind: "work",
                    id: work_id.to_string(),
                });
            };
            if let Some(Value::String(title)) = fields.get("title") {
                work.title = title.clone();
            }
            work.updated_at = now;
            Ok(work.clone())
        }
        async fn archive(&self, _tx: &mut FakeTx, work_id: Uuid) -> Result<Work> {
            let mut guard = self.0.lock().expect("lock");
            let now = Utc::now();
            let target = if guard.work.as_ref().is_some_and(|work| work.id == work_id) {
                guard.work.as_mut()
            } else {
                guard.works.get_mut(&work_id)
            };
            let Some(work) = target else {
                return Err(Error::NotFound {
                    kind: "work",
                    id: work_id.to_string(),
                });
            };
            work.status = WorkStatus::Archived;
            work.archived_at = Some(now);
            Ok(work.clone())
        }
    }

    impl<'a> WorkRevisionRepo for Shared<'a> {
        type Tx = FakeTx;
        async fn get(&self, revision_id: Uuid) -> Result<Option<WorkRevision>> {
            let guard = self.0.lock().expect("lock");
            if guard.fail_rev_get {
                return Err(Error::Storage("rev_get injected".to_owned()));
            }
            if guard
                .revision
                .as_ref()
                .is_some_and(|rev| rev.id == revision_id)
            {
                return Ok(guard.revision.clone());
            }
            if guard.created.id == revision_id {
                return Ok(Some(guard.created.clone()));
            }
            Ok(guard.revisions.get(&revision_id).cloned())
        }
        async fn insert(
            &self,
            _tx: &mut FakeTx,
            draft: marginalia_types::works::WorkRevisionDraft,
        ) -> Result<WorkRevision> {
            let mut guard = self.0.lock().expect("lock");
            let now = Utc::now();
            let revision = WorkRevision {
                id: Uuid::new_v4(),
                work_id: draft.work_id,
                revision_number: draft.revision_number,
                parent_revision_id: draft.parent_revision_id,
                state: RevisionState::Draft,
                message: draft.message,
                content_hash: None,
                created_by: draft.created_by,
                created_at: now,
                frozen_at: None,
                published_at: None,
                metadata: draft.metadata,
            };
            guard.revisions.insert(revision.id, revision.clone());
            Ok(revision)
        }
        async fn latest(&self, work_id: Uuid) -> Result<Option<WorkRevision>> {
            let guard = self.0.lock().expect("lock");
            let mut candidates: Vec<WorkRevision> = guard
                .revision
                .clone()
                .into_iter()
                .chain(std::iter::once(guard.created.clone()))
                .chain(guard.revisions.values().cloned())
                .filter(|rev| rev.work_id == work_id)
                .collect();
            candidates.sort_by_key(|rev| rev.revision_number);
            Ok(candidates.pop())
        }
        async fn copy_forward(&self, _tx: &mut FakeTx, _revision_id: Uuid) -> Result<WorkRevision> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_copy_forward {
                return Err(Error::Storage("copy_forward injected".to_owned()));
            }
            guard.seed_rows();
            Ok(guard.created.clone())
        }
        async fn set_message(
            &self,
            _tx: &mut FakeTx,
            revision_id: Uuid,
            message: &str,
        ) -> Result<WorkRevision> {
            let mut guard = self.0.lock().expect("lock");
            let target = if guard.created.id == revision_id {
                &mut guard.created
            } else if let Some(rev) = guard.revisions.get_mut(&revision_id) {
                rev
            } else {
                return Err(Error::NotFound {
                    kind: "work_revision",
                    id: revision_id.to_string(),
                });
            };
            target.message = Some(message.to_owned());
            Ok(target.clone())
        }
        async fn freeze(
            &self,
            _tx: &mut FakeTx,
            revision_id: Uuid,
            content_hash: &[u8],
        ) -> Result<WorkRevision> {
            let mut guard = self.0.lock().expect("lock");
            let now = Utc::now();
            let target = if guard.created.id == revision_id {
                &mut guard.created
            } else if let Some(rev) = guard.revisions.get_mut(&revision_id) {
                rev
            } else {
                return Err(Error::NotFound {
                    kind: "work_revision",
                    id: revision_id.to_string(),
                });
            };
            target.state = RevisionState::Frozen;
            target.content_hash = Some(content_hash.to_owned());
            target.frozen_at = Some(now);
            Ok(target.clone())
        }
        async fn publish(&self, _tx: &mut FakeTx, revision_id: Uuid) -> Result<WorkRevision> {
            let mut guard = self.0.lock().expect("lock");
            let now = Utc::now();
            let target = if guard.created.id == revision_id {
                &mut guard.created
            } else if let Some(rev) = guard.revisions.get_mut(&revision_id) {
                rev
            } else {
                return Err(Error::NotFound {
                    kind: "work_revision",
                    id: revision_id.to_string(),
                });
            };
            target.state = RevisionState::Published;
            target.published_at = Some(now);
            Ok(target.clone())
        }
        async fn supersede(&self, _tx: &mut FakeTx, revision_id: Uuid) -> Result<WorkRevision> {
            let mut guard = self.0.lock().expect("lock");
            let target = if guard.created.id == revision_id {
                &mut guard.created
            } else if let Some(rev) = guard.revisions.get_mut(&revision_id) {
                rev
            } else {
                return Err(Error::NotFound {
                    kind: "work_revision",
                    id: revision_id.to_string(),
                });
            };
            target.state = RevisionState::Superseded;
            Ok(target.clone())
        }
    }

    impl<'a> WorkBlockRepo for Shared<'a> {
        type Tx = FakeTx;
        async fn upsert(
            &self,
            _tx: &mut FakeTx,
            revision_id: Uuid,
            draft: WorkBlockDraft,
            expected_updated_at: Option<chrono::DateTime<Utc>>,
        ) -> Result<WorkBlock> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_upsert {
                return Err(Error::Storage("upsert injected".to_owned()));
            }
            guard.upserts.push((draft.clone(), expected_updated_at));
            let now = Utc::now();
            let key = draft.block_key.to_string();
            if let Some(old) = guard.rows.get(&key) {
                let updated = WorkBlock {
                    id: old.id,
                    revision_id,
                    block_key: draft.block_key,
                    parent_id: draft.parent_id,
                    position: draft.position,
                    block_type: draft.block_type.clone(),
                    title: draft.title.clone(),
                    body_markdown: draft.body_markdown.clone(),
                    attributes: draft.attributes.clone(),
                    created_at: old.created_at,
                    updated_at: now,
                };
                guard.rows.insert(key, updated.clone());
                Ok(updated)
            } else {
                let inserted = WorkBlock {
                    id: Uuid::new_v4(),
                    revision_id,
                    block_key: draft.block_key,
                    parent_id: draft.parent_id,
                    position: draft.position,
                    block_type: draft.block_type,
                    title: draft.title,
                    body_markdown: draft.body_markdown,
                    attributes: draft.attributes,
                    created_at: now,
                    updated_at: now,
                };
                guard.rows.insert(key, inserted.clone());
                Ok(inserted)
            }
        }
        async fn tree(&self, _revision_id: Uuid) -> Result<Vec<WorkBlock>> {
            let guard = self.0.lock().expect("lock");
            if guard.fail_tree {
                return Err(Error::Storage("tree injected".to_owned()));
            }
            Ok(guard.tree.clone())
        }
        async fn get(&self, block_id: Uuid) -> Result<Option<WorkBlock>> {
            let guard = self.0.lock().expect("lock");
            if let Some(row) = guard.rows.values().find(|row| row.id == block_id) {
                return Ok(Some(row.clone()));
            }
            Ok(guard.tree.iter().find(|row| row.id == block_id).cloned())
        }
        async fn by_key(&self, _revision_id: Uuid, block_key: Uuid) -> Result<Option<WorkBlock>> {
            let guard = self.0.lock().expect("lock");
            if let Some(row) = guard.rows.get(&block_key.to_string()) {
                return Ok(Some(row.clone()));
            }
            Ok(guard
                .tree
                .iter()
                .find(|row| row.block_key == block_key)
                .cloned())
        }
        async fn by_key_in_tx(
            &self,
            _tx: &mut FakeTx,
            _revision_id: Uuid,
            block_key: Uuid,
        ) -> Result<Option<WorkBlock>> {
            let mut guard = self.0.lock().expect("lock");
            guard.by_key_calls += 1;
            if guard.fail_by_key_on_call == Some(guard.by_key_calls) {
                return Err(Error::Storage("by_key_in_tx injected".to_owned()));
            }
            if guard.hidden_keys.contains(&block_key) {
                return Ok(None);
            }
            Ok(guard.rows.get(&block_key.to_string()).cloned())
        }
        async fn delete(&self, _tx: &mut FakeTx, block_id: Uuid) -> Result<()> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_delete {
                return Err(Error::Storage("delete injected".to_owned()));
            }
            guard.deletes.push(block_id);
            guard.rows.retain(|_, row| row.id != block_id);
            Ok(())
        }
    }

    impl<'a> CitationRepo for Shared<'a> {
        type Tx = FakeTx;
        async fn insert_occurrence(
            &self,
            _tx: &mut FakeTx,
            draft: marginalia_types::citations::OccurrenceDraft,
        ) -> Result<CitationOccurrence> {
            let mut guard = self.0.lock().expect("lock");
            let occurrence = CitationOccurrence {
                id: Uuid::new_v4(),
                citation_key: draft.citation_key,
                block_id: draft.block_id,
                placement: draft.placement,
                intent: draft.intent,
                note: draft.note,
                created_at: Utc::now(),
            };
            guard.occurrences.push(occurrence.clone());
            Ok(occurrence)
        }
        async fn insert_item(
            &self,
            _tx: &mut FakeTx,
            draft: marginalia_types::citations::CitationItemDraft,
        ) -> Result<CitationItem> {
            let mut guard = self.0.lock().expect("lock");
            let item = CitationItem {
                occurrence_id: draft.occurrence_id,
                position: draft.position,
                edition_id: draft.edition_id,
                edition_key: draft.edition_key,
                source_span_id: draft.source_span_id,
                quoted_text: draft.quoted_text,
                verify_status: draft.verify_status,
                verified_at: None,
                locator: draft.locator,
                prefix: draft.prefix,
                suffix: draft.suffix,
                suppress_author: draft.suppress_author,
            };
            guard.items.push(item.clone());
            Ok(item)
        }
        async fn for_block(&self, block_id: Uuid) -> Result<Vec<BlockCitations>> {
            let guard = self.0.lock().expect("lock");
            Ok(guard
                .attached
                .iter()
                .filter(|entry| entry.occurrence.block_id == block_id)
                .cloned()
                .collect())
        }
        async fn for_revision(&self, _revision_id: Uuid) -> Result<Vec<BlockCitations>> {
            let guard = self.0.lock().expect("lock");
            if guard.fail_for_revision {
                return Err(Error::Storage("for_revision injected".to_owned()));
            }
            Ok(guard.attached.clone())
        }
        async fn by_key(
            &self,
            _revision_id: Uuid,
            citation_key: Uuid,
        ) -> Result<Option<BlockCitations>> {
            let guard = self.0.lock().expect("lock");
            Ok(guard
                .attached
                .iter()
                .find(|entry| entry.occurrence.citation_key == citation_key)
                .cloned())
        }
        async fn citing_span(&self, span_id: Uuid) -> Result<Vec<BlockCitations>> {
            let guard = self.0.lock().expect("lock");
            Ok(guard
                .attached
                .iter()
                .filter(|entry| {
                    entry
                        .items
                        .iter()
                        .any(|item| item.source_span_id == Some(span_id))
                })
                .cloned()
                .collect())
        }
        async fn citing_key(&self, edition_key: &str) -> Result<Vec<BlockCitations>> {
            let guard = self.0.lock().expect("lock");
            Ok(guard
                .attached
                .iter()
                .filter(|entry| {
                    entry
                        .items
                        .iter()
                        .any(|item| item.edition_key.as_deref() == Some(edition_key))
                })
                .cloned()
                .collect())
        }
    }
    impl<'a> WorkLinkRepo for Shared<'a> {
        type Tx = FakeTx;
        async fn add_source_link(
            &self,
            _tx: &mut FakeTx,
            draft: marginalia_types::works::BlockSourceLinkDraft,
        ) -> Result<marginalia_types::works::BlockSourceLink> {
            let mut guard = self.0.lock().expect("lock");
            let link = marginalia_types::works::BlockSourceLink {
                block_id: draft.block_id,
                source_span_id: draft.source_span_id,
                relation: draft.relation,
                confidence: draft.confidence,
                note: draft.note,
                created_at: Utc::now(),
            };
            guard.source_links.push(link.clone());
            Ok(link)
        }
        async fn add_entity_link(
            &self,
            _tx: &mut FakeTx,
            draft: marginalia_types::works::BlockEntityLinkDraft,
        ) -> Result<marginalia_types::works::BlockEntityLink> {
            let mut guard = self.0.lock().expect("lock");
            let link = marginalia_types::works::BlockEntityLink {
                block_id: draft.block_id,
                entity_id: draft.entity_id,
                relation: draft.relation,
                surface_form: draft.surface_form,
                created_at: Utc::now(),
            };
            guard.entity_links.push(link.clone());
            Ok(link)
        }
        async fn for_block(&self, block_id: Uuid) -> Result<BlockLinks> {
            let guard = self.0.lock().expect("lock");
            Ok(BlockLinks {
                sources: guard
                    .source_links
                    .iter()
                    .filter(|link| link.block_id == block_id)
                    .cloned()
                    .collect(),
                entities: guard
                    .entity_links
                    .iter()
                    .filter(|link| link.block_id == block_id)
                    .cloned()
                    .collect(),
            })
        }
        async fn for_span(
            &self,
            span_id: Uuid,
        ) -> Result<Vec<marginalia_types::works::BlockSourceLink>> {
            let guard = self.0.lock().expect("lock");
            Ok(guard
                .source_links
                .iter()
                .filter(|link| link.source_span_id == span_id)
                .cloned()
                .collect())
        }
        async fn for_entity(
            &self,
            entity_id: Uuid,
            relation: &str,
        ) -> Result<Vec<marginalia_types::works::BlockEntityLink>> {
            let guard = self.0.lock().expect("lock");
            Ok(guard
                .entity_links
                .iter()
                .filter(|link| link.entity_id == entity_id && link.relation == relation)
                .cloned()
                .collect())
        }
    }

    impl<'a> SourceSpanRepo for Shared<'a> {
        type Tx = FakeTx;
        async fn resolve(
            &self,
            _tx: &mut FakeTx,
            document_id: Uuid,
            char_start: i64,
            char_end: i64,
        ) -> Result<SourceSpan> {
            let mut guard = self.0.lock().expect("lock");
            let span = SourceSpan {
                id: Uuid::new_v4(),
                document_id,
                char_start,
                char_end,
                quoted_text: String::new(),
                parser: None,
                parser_version: None,
                passage_id: None,
                created_at: Utc::now(),
            };
            guard.spans.insert(span.id, span.clone());
            Ok(span)
        }
        async fn get(&self, span_id: Uuid) -> Result<Option<SourceSpan>> {
            Ok(self.0.lock().expect("lock").spans.get(&span_id).cloned())
        }
        async fn for_document(&self, document_id: Uuid) -> Result<Vec<SourceSpan>> {
            let guard = self.0.lock().expect("lock");
            Ok(guard
                .spans
                .values()
                .filter(|span| span.document_id == document_id)
                .cloned()
                .collect())
        }
        async fn stale(&self, limit: i64) -> Result<Vec<SourceSpan>> {
            let guard = self.0.lock().expect("lock");
            let mut spans: Vec<SourceSpan> = guard.spans.values().cloned().collect();
            spans.sort_by_key(|span| span.created_at);
            spans.truncate(limit.max(0) as usize);
            Ok(spans)
        }
    }

    fn service<'a>(
        fakes: &'a Mutex<Fakes>,
    ) -> WorkExportService<
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        FakeTx,
        Shared<'a>,
    > {
        let shared = Shared(fakes);
        WorkExportService::new(shared, shared, shared, shared, shared, shared, shared)
    }

    /// `test_drafting_loop`: an edited paragraph updates, a comment-less
    /// paragraph adds, the untouched heading stays out of the diff.
    #[test]
    fn test_import_updates_and_adds() {
        let fakes = Mutex::new(Fakes::rig());
        let exported = fakes.lock().expect("lock").markdown();
        let edited = exported
            .replace("The prophets speak.", "The prophets speak twice.")
            .replace(
                "{{cite:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}}.\n",
                "{{cite:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}}.\n\nA new claim.\n",
            );
        let diff = block_on(service(&fakes).import_draft(Some("deror"), None, &edited, false))
            .expect("import");
        assert_eq!(diff.revision_number, 2);
        assert!(!diff.dry_run);
        let kinds: HashMap<String, String> = diff
            .changes
            .iter()
            .map(|change| (change.block_key.clone(), change.change.clone()))
            .collect();
        assert_eq!(kinds.get(KEY_B), Some(&"updated".to_owned()));
        assert_eq!(kinds.len(), 2);
        assert!(!kinds.contains_key(KEY_A));
        assert!(kinds.values().any(|change| change == "added"));
        let guard = fakes.lock().expect("lock");
        assert_eq!(guard.tx_events, vec!["begin", "commit"]);
        assert_eq!(
            guard.set_current,
            vec![(uuid(WORK_ID), uuid("99999999-0000-0000-0000-000000000000"))]
        );
        // Updates carry the veteran timestamp; inserts carry none.
        let mut saw_expected = false;
        let mut saw_none = false;
        for (_, expected) in &guard.upserts {
            if expected.is_some() {
                saw_expected = true;
            } else {
                saw_none = true;
            }
        }
        assert!(saw_expected && saw_none);
    }

    /// The dry run computes the same diff but rolls back and never promotes
    /// the revision — `test_drafting_loop`'s second half.
    #[test]
    fn test_import_dry_run_rolls_back() {
        let fakes = Mutex::new(Fakes::rig());
        let exported = fakes.lock().expect("lock").markdown();
        let edited = exported.replace("The prophets speak.", "The prophets speak twice.");
        let diff = block_on(service(&fakes).import_draft(Some("deror"), None, &edited, true))
            .expect("dry run");
        assert!(diff.dry_run);
        assert_eq!(diff.changes.len(), 1);
        assert_eq!(diff.changes[0].change, "updated");
        let guard = fakes.lock().expect("lock");
        assert_eq!(guard.tx_events, vec!["begin", "rollback"]);
        assert!(guard.set_current.is_empty());
    }

    /// §5.4 round-trip contract: export then import with no edits is a
    /// no-op new revision — same keys, empty diff.
    #[test]
    fn test_import_unedited_export_is_noop() {
        let fakes = Mutex::new(Fakes::rig());
        let exported = fakes.lock().expect("lock").markdown();
        let diff = block_on(service(&fakes).import_draft(Some("deror"), None, &exported, false))
            .expect("import");
        assert_eq!(diff.revision_number, 2);
        assert!(diff.changes.is_empty());
        let guard = fakes.lock().expect("lock");
        assert_eq!(guard.tx_events, vec!["begin", "commit"]);
        assert!(guard.upserts.is_empty());
        assert!(guard.deletes.is_empty());
    }

    /// `test_dangling_import_refuses`: nothing is written and the revision
    /// stays put.
    #[test]
    fn test_import_dangling_marker_refuses() {
        let fakes = Mutex::new(Fakes::rig());
        let exported = fakes.lock().expect("lock").markdown();
        let forged =
            format!("{exported}\nGhost {{{{cite:99999999-9999-9999-9999-999999999999}}}}.\n");
        let error = block_on(service(&fakes).import_draft(Some("deror"), None, &forged, false))
            .expect_err("must refuse");
        assert!(matches!(
            error,
            ImportError::Refused(refused) if refused.rule_id == "AUTH_CITATION_MARKER_DANGLING"
        ));
        let guard = fakes.lock().expect("lock");
        assert_eq!(guard.tx_events, vec!["begin", "rollback"]);
        assert!(guard.upserts.is_empty());
        assert!(guard.set_current.is_empty());
    }

    #[test]
    fn test_import_invalid_marker_refuses() {
        let fakes = Mutex::new(Fakes::rig());
        let exported = fakes.lock().expect("lock").markdown();
        let forged = format!("{exported}\nGhost {{{{cite:c1}}}}.\n");
        let error = block_on(service(&fakes).import_draft(Some("deror"), None, &forged, false))
            .expect_err("must refuse");
        assert!(matches!(
            error,
            ImportError::Refused(refused) if refused.message == "Marker {{cite:c1}} names no citation: keys are UUIDs"
        ));
    }

    /// `test_import_for_wrong_work_is_invalid`: the front-work check fires
    /// before any transaction opens.
    #[test]
    fn test_import_wrong_work_mismatch() {
        let fakes = Mutex::new(Fakes::rig());
        let exported = fakes.lock().expect("lock").markdown();
        let renamed = exported.replace("work: deror", "work: other");
        let error = block_on(service(&fakes).import_draft(Some("deror"), None, &renamed, false))
            .expect_err("must fail");
        assert!(matches!(
            error,
            ImportError::Failed(Error::Validation(message))
                if message == "Import names work 'other', not 'deror'"
        ));
        assert!(fakes.lock().expect("lock").tx_events.is_empty());
    }

    #[test]
    fn test_import_missing_work_is_not_found() {
        let fakes = Mutex::new(Fakes::rig());
        let error = block_on(service(&fakes).import_draft(
            Some("missing"),
            None,
            "---\nwork: missing\n---\n",
            false,
        ))
        .expect_err("must fail");
        assert!(matches!(
            error,
            ImportError::Failed(Error::NotFound { kind: "work", .. })
        ));
        assert!(fakes.lock().expect("lock").tx_events.is_empty());
    }

    #[test]
    fn test_import_missing_current_is_not_found() {
        let fakes = Mutex::new(Fakes::rig());
        fakes
            .lock()
            .expect("lock")
            .work
            .as_mut()
            .expect("work")
            .current_revision_id = None;
        let error = block_on(service(&fakes).import_draft(
            Some("deror"),
            None,
            "---\nwork: deror\n---\n",
            false,
        ))
        .expect_err("must fail");
        assert!(matches!(
            error,
            ImportError::Failed(Error::NotFound { kind, id })
                if kind == "work_revision" && id == "current of deror"
        ));
    }

    /// Dropping a block comment deletes its row.
    #[test]
    fn test_import_deletes_missing_blocks() {
        let fakes = Mutex::new(Fakes::rig());
        let markdown = format!("---\nwork: deror\n---\n\n<!-- block:{KEY_A} -->\n## Release\n");
        let diff = block_on(service(&fakes).import_draft(Some("deror"), None, &markdown, false))
            .expect("import");
        assert_eq!(diff.changes.len(), 1);
        assert_eq!(diff.changes[0].block_key, KEY_B);
        assert_eq!(diff.changes[0].change, "deleted");
        assert_eq!(fakes.lock().expect("lock").deletes, vec![uuid(ROW_B)]);
    }

    /// Swapped siblings move; the diff follows parsed order.
    #[test]
    fn test_import_reorder_moves_siblings() {
        let fakes = Mutex::new(Fakes::rig());
        {
            let mut guard = fakes.lock().expect("lock");
            guard.tree.push(row(RowParams {
                id: "77777777-0000-0000-0000-000000000000",
                key: "88888888-0000-0000-0000-000000000000",
                parent_id: None,
                position: 1,
                block_type: "paragraph",
                title: None,
                body: "Second root.",
                attributes: Map::new(),
            }));
        }
        let markdown = format!(
            "---\nwork: deror\n---\n\n<!-- block:88888888-0000-0000-0000-000000000000 -->\nSecond root.\n\n<!-- block:{KEY_A} -->\n## Release\n"
        );
        let diff = block_on(service(&fakes).import_draft(Some("deror"), None, &markdown, false))
            .expect("import");
        // The cited paragraph vanished (deleted); both roots moved.
        let kinds: Vec<(&str, &str)> = diff
            .changes
            .iter()
            .map(|change| (change.block_key.as_str(), change.change.as_str()))
            .collect();
        assert!(kinds.contains(&("88888888-0000-0000-0000-000000000000", "moved")));
        assert!(kinds.contains(&(KEY_A, "moved")));
        assert!(kinds.contains(&(KEY_B, "deleted")));
    }

    /// Double-quoted escapes with backslash continuations, nested plain
    /// wrapping, and plain scalars carrying quotes — all captured from
    /// CPython for the same fixtures.
    #[test]
    fn test_yaml_double_quoted_and_nested_wrap() {
        let quoted =
            "The quick brown fox jumps over the lazy dog and then keeps going on and on forever.";
        let mut view = cited_view("T");
        let occ = occurrence(uuid(ROW_B));
        let mut long = item(occ.id);
        long.quoted_text = Some(quoted.to_owned());
        view.blocks[1].citations = vec![BlockCitations {
            occurrence: occ,
            items: vec![long],
        }];
        let rendered = render_markdown(&view);
        assert!(rendered.contains(
            "  quoted_text: The quick brown fox jumps over the lazy dog and then keeps going on\n    and on forever.\n"
        ));
        for (title, expected) in [
            ("a\tb", "title: \"a\\tb\"\n"),
            (
                "has \"dq\" and tab\there plus a very long tail of words to force the width split rule",
                "title: \"has \\\"dq\\\" and tab\\there plus a very long tail of words to force the width\\\n  \\ split rule\"\n",
            ),
            (
                "it's a very long title with a quote that goes on and on and on and on and on and on",
                "title: it's a very long title with a quote that goes on and on and on and on and on\n  and on\n",
            ),
        ] {
            let rendered = render_markdown(&cited_view(title));
            let line: String = rendered
                .lines()
                .skip_while(|line| !line.starts_with("title:"))
                .take_while(|line| !line.starts_with("type:"))
                .collect::<Vec<_>>()
                .join("\n")
                + "\n";
            assert_eq!(line, expected, "title {title:?}");
        }
    }
    /// Nested collections, explicit `?` keys, and nested double-quoted
    /// continuations inside locators — all captured from CPython.
    #[test]
    fn test_yaml_nested_locator_structures() {
        let structures = [
            (
                Map::from_iter([
                    ("a".to_owned(), Value::Array(vec![])),
                    ("b".to_owned(), Value::Object(Map::new())),
                    (
                        "c".to_owned(),
                        Value::Array(vec![Value::from(1), Value::from(2)]),
                    ),
                    (
                        "d".to_owned(),
                        Value::Object(Map::from_iter([(
                            "e".to_owned(),
                            Value::String("f".to_owned()),
                        )])),
                    ),
                ]),
                "  locator:\n    a: []\n    b: {}\n    c:\n    - 1\n    - 2\n    d:\n      e: f\n",
            ),
            (
                Map::from_iter([(":weird key".to_owned(), Value::from(1))]),
                "  locator:\n    :weird key: 1\n",
            ),
            (
                Map::from_iter([("line1\nline2".to_owned(), Value::from(1))]),
                "  locator:\n    ? 'line1\n\n      line2'\n    : 1\n",
            ),
            (
                Map::from_iter([(
                    "outer".to_owned(),
                    Value::Array(vec![
                        Value::Array(vec![Value::from(1), Value::from(2)]),
                        Value::Array(vec![Value::from(3)]),
                    ]),
                )]),
                "  locator:\n    outer:\n    - - 1\n      - 2\n    - - 3\n",
            ),
        ];
        for (locator, expected) in structures {
            let mut view = cited_view("T");
            let occ = occurrence(uuid(ROW_B));
            let mut located = item(occ.id);
            located.locator = locator;
            view.blocks[1].citations = vec![BlockCitations {
                occurrence: occ,
                items: vec![located],
            }];
            let rendered = render_markdown(&view);
            assert!(rendered.contains(expected), "locator shape:\n{rendered}");
        }
        // Nested double-quoted scalars continue with a backslash at the
        // deeper indent.
        let mut view = cited_view("T");
        let occ = occurrence(uuid(ROW_B));
        let mut quoted = item(occ.id);
        quoted.quoted_text = Some(
            "a\tb plus a very long tail of words to force the width split rule here and onward"
                .to_owned(),
        );
        view.blocks[1].citations = vec![BlockCitations {
            occurrence: occ,
            items: vec![quoted],
        }];
        let rendered = render_markdown(&view);
        assert!(rendered.contains(
            "  quoted_text: \"a\\tb plus a very long tail of words to force the width split rule\\\n    \\ here and onward\"\n"
        ));
    }
    use marginalia_types::citations::{CitationItemDraft, OccurrenceDraft};
    use marginalia_types::works::{
        BlockEntityLinkDraft, BlockSourceLinkDraft, WorkDraft, WorkRevisionDraft,
    };

    /// The `title:` paragraph of the front matter: from the title line
    /// through its continuations, stopping at the next top-level key.
    fn title_para(rendered: &str) -> String {
        rendered
            .lines()
            .skip_while(|line| !line.starts_with("title:"))
            .take_while(|line| !line.starts_with("type:"))
            .collect::<Vec<_>>()
            .join("\n")
            + "\n"
    }

    #[test]
    fn test_import_error_display() {
        let refused = ImportRefused {
            rule_id: "AUTH_X".to_owned(),
            message: "Marker {{cite:c1}} names no citation".to_owned(),
            detail: None,
        };
        assert_eq!(format!("{refused}"), "Marker {{cite:c1}} names no citation");
        assert_eq!(
            format!("{}", ImportError::Refused(refused.clone())),
            "Marker {{cite:c1}} names no citation"
        );
        let failed = ImportError::Failed(Error::Validation("bad".to_owned()));
        assert_eq!(format!("{failed}"), "data validation failed: bad");
        assert_eq!(
            format!(
                "{}",
                ImportError::from(Error::NotFound {
                    kind: "work",
                    id: "x".to_owned()
                })
            ),
            "work not found: x"
        );
    }

    #[test]
    fn test_py_splitlines_boundaries() {
        // Every `splitlines` boundary through the block scanner: CRLF, lone
        // CR, vertical tab, form feed, NEL, and U+2028/2029 each split, with
        // no trailing empty item for the final newline — mirroring
        // `"a\n".splitlines() == ["a"]`.
        assert_eq!(
            py_splitlines("a\r\nb\rc\x0bd\x0ce\x1cf\u{85}g\u{2028}h\u{2029}i\n"),
            vec!["a", "b", "c", "d", "e", "f", "g", "h", "i"]
        );
        assert_eq!(py_splitlines("a\n"), vec!["a"]);
        let (_, parsed) =
            parse_markdown("---\nwork: deror\n---\n\npara one\r\n\r\npara two\n").expect("parse");
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].body, "para one");
        assert_eq!(parsed[1].body, "para two");
        // A vertical tab inside the body splits like any other boundary.
        let (_, split) = parse_markdown("---\nwork: deror\n---\n\na\x0bb\n").expect("parse");
        assert_eq!(split.len(), 1);
        assert_eq!(split[0].body, "a\nb");
        // No trailing newline: the remainder still forms the last line.
        let (_, tail) = parse_markdown("---\nwork: deror\n---\n\nText.").expect("parse");
        assert_eq!(tail.len(), 1);
        assert_eq!(tail[0].body, "Text.");
        // An empty body scans to no blocks at all.
        let (front, none) = parse_markdown("---\nwork: deror\n---\n").expect("parse");
        assert_eq!(front.get("work"), Some(&Value::String("deror".to_owned())));
        assert!(none.is_empty());
    }

    /// Float locator values ride `json_to_node` into `py_repr_float`:
    /// u64 saturation, plain and scientific magnitudes, and signed zero —
    /// each line equals PyYAML's `safe_dump` for the same mapping.
    #[test]
    fn test_yaml_locator_float_shapes() {
        let mut view = cited_view("T");
        let occ = occurrence(uuid(ROW_B));
        let mut located = item(occ.id);
        located.locator = Map::from_iter([
            ("plain".to_owned(), Value::from(1.5)),
            ("big".to_owned(), Value::from(1e300)),
            ("exp".to_owned(), Value::from(1e17)),
            ("small".to_owned(), Value::from(1.5e-7)),
            ("negzero".to_owned(), Value::from(-0.0)),
            ("huge".to_owned(), Value::from(u64::MAX)),
            // No infinities: a serde_json `Number` can never hold one (the
            // parser rejects `1e400` with "number out of range" and
            // `from_f64`/`Value::from` refuse or null non-finite), so the
            // `.inf`/`-.inf` spellings have no locator spelling to test.
        ]);
        view.blocks[1].citations = vec![BlockCitations {
            occurrence: occ,
            items: vec![located],
        }];
        let rendered = render_markdown(&view);
        assert!(rendered.contains(
            "  locator:\n    plain: 1.5\n    big: 1.0e+300\n    exp: 1.0e+17\n    small: 1.5e-07\n    negzero: -0.0\n    huge: 9223372036854775807\n"
        ));
    }
    /// Single-quoted line breaks re-emit the break plus the two-space
    /// continuation, quote doubling, and width wrapping — each paragraph
    /// equals PyYAML's `safe_dump` for the same title.
    #[test]
    fn test_yaml_single_quoted_breaks_doubling_wrap() {
        for (title, expected) in [
            ("a\u{85}b", "title: 'a\u{85}  b'\n"),
            ("a\u{2028}b", "title: 'a\u{2028}  b'\n"),
            ("a\u{2029}b", "title: 'a\u{2029}  b'\n"),
            ("it's: here", "title: 'it''s: here'\n"),
            ("d'Artagnan: x", "title: 'd''Artagnan: x'\n"),
        ] {
            assert_eq!(
                title_para(&render_markdown(&cited_view(title))),
                expected,
                "title {title:?}"
            );
        }
        // A long single-quoted title wraps at width 80 with the two-space
        // continuation, per CPython for the same 25-word title.
        let title = format!("Release: {}", "word ".repeat(25));
        let expected = format!(
            "title: 'Release: {}\n  {}'\n",
            "word ".repeat(13).trim_end(),
            "word ".repeat(12)
        );
        assert_eq!(title_para(&render_markdown(&cited_view(&title))), expected);
    }

    /// `\x`/`\u`/`\U` escapes for non-printables, each paragraph equal to
    /// PyYAML's `safe_dump` for the same title.
    #[test]
    fn test_yaml_nonprintable_escapes() {
        for (title, expected) in [
            ("x\x7fy", "title: \"x\\x7Fy\"\n"),
            ("\u{feff}", "title: \"\\uFEFF\"\n"),
            ("\u{10ffff}", "title: \"\\U0010FFFF\"\n"),
            ("\u{9f}", "title: \"\\x9F\"\n"),
        ] {
            assert_eq!(
                title_para(&render_markdown(&cited_view(title))),
                expected,
                "title {title:?}"
            );
        }
        // An escape at the width edge backslash-continues immediately (the
        // inverted-window fallback), per CPython for the same 75-x title.
        let pad = "x".repeat(75);
        let title = format!("{pad}\tyyyyyyyyyy");
        let expected = format!("title: \"{pad}\\t\\\n  yyyyyyyyyy\"\n");
        assert_eq!(title_para(&render_markdown(&cited_view(&title))), expected);
    }

    /// Every `ESCAPE_REPLACEMENTS` arm: each control char paired with `\x01`
    /// so the scalar can only double-quote, each paragraph equal to
    /// PyYAML's `safe_dump` for the same title.
    #[test]
    fn test_yaml_escape_replacements() {
        for (title, expected) in [
            ("\x00\x01", "title: \"\\0\\x01\"\n"),
            ("\x07\x01", "title: \"\\a\\x01\"\n"),
            ("\x08\x01", "title: \"\\b\\x01\"\n"),
            ("\t\x01", "title: \"\\t\\x01\"\n"),
            ("\n\x01", "title: \"\\n\\x01\"\n"),
            ("\x0b\x01", "title: \"\\v\\x01\"\n"),
            ("\x0c\x01", "title: \"\\f\\x01\"\n"),
            ("\r\x01", "title: \"\\r\\x01\"\n"),
            ("\x1b\x01", "title: \"\\e\\x01\"\n"),
            ("\"\x01", "title: \"\\\"\\x01\"\n"),
            ("\\\x01", "title: \"\\\\\\x01\"\n"),
            ("\u{85}\x01", "title: \"\\N\\x01\"\n"),
            ("\u{2028}\x01", "title: \"\\L\\x01\"\n"),
            ("\u{2029}\x01", "title: \"\\P\\x01\"\n"),
        ] {
            assert_eq!(
                title_para(&render_markdown(&cited_view(title))),
                expected,
                "title {title:?}"
            );
        }
    }

    /// Scalar-analysis indicator and whitespace edges, each paragraph equal
    /// to PyYAML's `safe_dump` for the same title.
    #[test]
    fn test_yaml_scalar_analysis_edges() {
        for (title, expected) in [
            ("---x", "title: '---x'\n"),
            ("...x", "title: '...x'\n"),
            ("#hashtag", "title: '#hashtag'\n"),
            ("*star", "title: '*star'\n"),
            ("? x", "title: '? x'\n"),
            ("a[b", "title: a[b\n"),
            ("a,b", "title: a,b\n"),
            ("a  b", "title: a  b\n"),
            ("a\n\nb", "title: 'a\n\n\n  b'\n"),
            ("\nabc", "title: '\n\n  abc'\n"),
            ("abc\n", "title: 'abc\n\n  '\n"),
            ("a\n b", "title: \"a\\n b\"\n"),
            ("a \nb", "title: \"a \\nb\"\n"),
            ("\tlead", "title: \"\\tlead\"\n"),
            ("trail\t", "title: \"trail\\t\"\n"),
        ] {
            assert_eq!(
                title_para(&render_markdown(&cited_view(title))),
                expected,
                "title {title:?}"
            );
        }
    }
    /// Datetime-shaped titles quote as `!!timestamp` (and near-misses stay
    /// plain), each paragraph equal to PyYAML's `safe_dump` for the title.
    #[test]
    fn test_yaml_timestamp_shapes() {
        for (title, expected) in [
            ("2001-02-03T04:05:06", "title: '2001-02-03T04:05:06'\n"),
            ("2001-02-03t04:05:06", "title: '2001-02-03t04:05:06'\n"),
            ("2001-02-03 04:05:06", "title: '2001-02-03 04:05:06'\n"),
            ("2001-02-03  04:05:06", "title: '2001-02-03  04:05:06'\n"),
            ("2001-02-03T04:05:06.5", "title: '2001-02-03T04:05:06.5'\n"),
            ("2001-02-03T04:05:06.", "title: '2001-02-03T04:05:06.'\n"),
            (
                "2001-02-03T04:05:06.123456789+02:30",
                "title: '2001-02-03T04:05:06.123456789+02:30'\n",
            ),
            ("2001-02-03T04:05:06Z", "title: '2001-02-03T04:05:06Z'\n"),
            (
                "2001-02-03T04:05:06+02:00",
                "title: '2001-02-03T04:05:06+02:00'\n",
            ),
            (
                "2001-02-03T04:05:06+02",
                "title: '2001-02-03T04:05:06+02'\n",
            ),
            ("2001-02", "title: 2001-02\n"),
            ("2001-02-", "title: 2001-02-\n"),
            ("2001-02-03T04:05:6", "title: 2001-02-03T04:05:6\n"),
            ("2001-02-03T04:05:xy", "title: 2001-02-03T04:05:xy\n"),
            ("2001-02-03T04:05:06!", "title: 2001-02-03T04:05:06!\n"),
            ("2001-02-03T04:05:06 ", "title: '2001-02-03T04:05:06 '\n"),
            ("2001-02-03 04:05", "title: 2001-02-03 04:05\n"),
            ("2001-02-03T04", "title: 2001-02-03T04\n"),
            ("2001-02-03T04:05:06+", "title: 2001-02-03T04:05:06+\n"),
            (
                "2001-02-03T04:05:06+2:5",
                "title: 2001-02-03T04:05:06+2:5\n",
            ),
            ("2001-02-03x04:05:06", "title: 2001-02-03x04:05:06\n"),
            ("20010203T040506", "title: 20010203T040506\n"),
        ] {
            assert_eq!(
                title_para(&render_markdown(&cited_view(title))),
                expected,
                "title {title:?}"
            );
        }
    }

    /// Implicit-resolution quoting across the `!!bool`/`!!null`/`!!value`/
    /// `!!merge`/`!!int`/`!!float` shapes, each paragraph equal to PyYAML's
    /// `safe_dump` for the title.
    #[test]
    fn test_yaml_implicit_resolution_battery() {
        for (title, expected) in [
            ("no", "title: 'no'\n"),
            ("Null", "title: 'Null'\n"),
            ("NULL", "title: 'NULL'\n"),
            ("nulL", "title: nulL\n"),
            ("~", "title: '~'\n"),
            ("=", "title: '='\n"),
            ("==", "title: ==\n"),
            ("<<", "title: '<<'\n"),
            ("<x", "title: <x\n"),
            ("0b101", "title: '0b101'\n"),
            ("0x1F", "title: '0x1F'\n"),
            ("017", "title: '017'\n"),
            ("0", "title: '0'\n"),
            ("+1", "title: '+1'\n"),
            ("-0", "title: '-0'\n"),
            ("1_2", "title: '1_2'\n"),
            ("1:20", "title: '1:20'\n"),
            ("1:20:30", "title: '1:20:30'\n"),
            ("1:2:30", "title: '1:2:30'\n"),
            ("1:205x", "title: 1:205x\n"),
            ("-1:20", "title: '-1:20'\n"),
            ("1:2:3", "title: '1:2:3'\n"),
            ("1:x", "title: 1:x\n"),
            (".Inf", "title: '.Inf'\n"),
            (".inf", "title: '.inf'\n"),
            (".NAN", "title: '.NAN'\n"),
            (".nan", "title: '.nan'\n"),
            (".NaN", "title: '.NaN'\n"),
            (".5", "title: '.5'\n"),
            (".567", "title: '.567'\n"),
            (".5e+3", "title: '.5e+3'\n"),
            (".5E+3", "title: '.5E+3'\n"),
            ("1.", "title: '1.'\n"),
            ("1.5e10", "title: 1.5e10\n"),
            ("1:2.5", "title: '1:2.5'\n"),
            ("1:20.5", "title: '1:20.5'\n"),
            ("1:20.5_0", "title: '1:20.5_0'\n"),
            ("0x1f", "title: '0x1f'\n"),
            ("12x", "title: 12x\n"),
            ("1.5e+10", "title: '1.5e+10'\n"),
            ("20010203", "title: '20010203'\n"),
            ("0o17", "title: 0o17\n"),
        ] {
            assert_eq!(
                title_para(&render_markdown(&cited_view(title))),
                expected,
                "title {title:?}"
            );
        }
    }

    /// Case variants of the float literals, straight at the classifiers:
    /// lowercase/uppercase infinity, uppercase exponents, and an
    /// underscore in a sexagesimal fraction.
    #[test]
    fn test_float_literal_case_variants() {
        let chars = |text: &str| text.chars().collect::<Vec<_>>();
        for text in [".inf", ".Inf", ".INF", ".nan", ".NaN", ".NAN"] {
            assert!(is_float_literal(&chars(text)), "{text}");
        }
        for text in [".5E+3", "1.5E+3", "1.5e+3", "1:20.5_0"] {
            assert!(is_float_literal(&chars(text)), "{text}");
        }
        assert!(is_exponent_suffix(&chars("E+3")));
        assert!(is_exponent_suffix(&chars("e-07")));
        // The `matches!` fall-through arms: a non-exponent trailer, a
        // non-`e` suffix head, and a non-digit sexagesimal fraction tail
        // are all rejected rather than classified.
        assert!(!is_exponent_suffix(&chars("x+3")));
        for text in [".5x", "1.5x", "1:20.5x"] {
            assert!(!is_float_literal(&chars(text)), "{text}");
        }
    }
    /// `int()` coercion over every JSON shape: exact CPython messages,
    /// u64 saturation, and float truncation with clamping.
    #[test]
    fn test_py_int_from_value_shapes() {
        assert_eq!(py_int_from_value(&Value::Bool(true)).expect("bool"), 1);
        assert_eq!(py_int_from_value(&Value::Bool(false)).expect("bool"), 0);
        assert_eq!(py_int_from_value(&Value::from(41)).expect("int"), 41);
        assert_eq!(
            py_int_from_value(&Value::from(u64::MAX)).expect("u64"),
            i64::MAX
        );
        assert_eq!(py_int_from_value(&Value::from(2.7)).expect("float"), 2);
        assert_eq!(py_int_from_value(&Value::from(-2.7)).expect("float"), -2);
        assert_eq!(
            py_int_from_value(&serde_json::from_str::<Value>("1e300").expect("finite"))
                .expect("clamp"),
            i64::MAX
        );
        assert_eq!(
            py_int_from_value(&serde_json::from_str::<Value>("-1e300").expect("finite"))
                .expect("clamp"),
            i64::MIN
        );
        assert_eq!(
            format!("{}", py_int_from_value(&Value::Null).expect_err("null")),
            "data validation failed: int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"
        );
        assert_eq!(
            format!(
                "{}",
                py_int_from_value(&Value::Array(vec![])).expect_err("list")
            ),
            "data validation failed: int() argument must be a string, a bytes-like object or a real number, not 'list'"
        );
        assert_eq!(
            format!(
                "{}",
                py_int_from_value(&Value::Object(Map::new())).expect_err("dict")
            ),
            "data validation failed: int() argument must be a string, a bytes-like object or a real number, not 'dict'"
        );
        // No infinity case: a serde_json `Number` can never hold one (see
        // the `py_repr_float` proof), so the infinity refusal has no
        // constructible input and its arm is deleted, not tested.
    }

    /// `int(str)` spellings: signs, padding, underscores, saturation —
    /// exact CPython accept/reject behavior with saturating extremes.
    #[test]
    fn test_py_int_from_str_spellings() {
        assert_eq!(py_int_from_str("+3").expect("sign"), 3);
        assert_eq!(py_int_from_str("-2").expect("sign"), -2);
        assert_eq!(py_int_from_str("  12  ").expect("padded"), 12);
        assert_eq!(py_int_from_str("007").expect("octal digits"), 7);
        assert_eq!(py_int_from_str("1_2").expect("underscores"), 12);
        assert_eq!(
            py_int_from_str(&"9".repeat(30)).expect("saturate"),
            i64::MAX
        );
        assert_eq!(
            py_int_from_str(&format!("-{}", "9".repeat(30))).expect("saturate"),
            i64::MIN
        );
        for bad in ["", "1__2", "1_", "_1", "1.5", "x", "+ 3", "--2"] {
            assert!(
                py_int_from_str(bad).is_err(),
                "int({bad:?}) must fail like CPython"
            );
        }
        assert_eq!(
            format!(
                "{}",
                py_int_from_str("1__2").expect_err("double underscore")
            ),
            "data validation failed: invalid literal for int() with base 10: '1__2'"
        );
    }
    /// Every revision state and every non-quotation intent renders its
    /// literal value into the export.
    #[test]
    fn test_render_states_and_intents() {
        for (state, expected) in [
            (RevisionState::Draft, "state: draft\n"),
            (RevisionState::Frozen, "state: frozen\n"),
            (RevisionState::Published, "state: published\n"),
            (RevisionState::Superseded, "state: superseded\n"),
        ] {
            let mut view = cited_view("T");
            view.revision.state = state;
            let line = render_markdown(&view)
                .lines()
                .find(|line| line.starts_with("state:"))
                .expect("state line")
                .to_owned()
                + "\n";
            assert_eq!(line, expected);
        }
        let intents = [
            Intent::Quotation,
            Intent::Translation,
            Intent::Support,
            Intent::Contrast,
            Intent::Background,
            Intent::Definition,
            Intent::Source,
            Intent::SeeAlso,
        ];
        let mut view = cited_view("T");
        view.blocks.clear();
        for (index, intent) in intents.into_iter().enumerate() {
            let block = row(RowParams {
                id: ROW_A,
                key: KEY_A,
                parent_id: None,
                position: index as i64,
                block_type: "paragraph",
                title: None,
                body: "Body.",
                attributes: Map::new(),
            });
            let occ = CitationOccurrence {
                id: uuid("77777777-7777-7777-7777-777777777777"),
                citation_key: uuid(&format!("aaaaaaaa-aaaa-aaaa-aaaa-{:012x}", index)),
                block_id: block.id,
                placement: Placement::BlockEnd,
                intent,
                note: None,
                created_at: Utc::now(),
            };
            view.blocks.push(AssembledBlock {
                block,
                parent_key: None,
                citations: vec![BlockCitations {
                    occurrence: occ.clone(),
                    items: vec![item(occ.id)],
                }],
                links: Some(BlockLinks::default()),
            });
        }
        let rendered = render_markdown(&view);
        for expected in [
            "intent: quotation\n",
            "intent: translation\n",
            "intent: support\n",
            "intent: contrast\n",
            "intent: background\n",
            "intent: definition\n",
            "intent: source\n",
            "intent: see_also\n",
        ] {
            assert!(
                rendered.contains(expected),
                "missing {expected}:\n{rendered}"
            );
        }
    }

    /// A fully-dressed citation row: span address, quote, verify status,
    /// edition key/id, and a filled locator, each on its own front line.
    #[test]
    fn test_render_full_citation_row() {
        let mut view = cited_view("T");
        let span_id = uuid("99999999-9999-9999-9999-999999999999");
        let edition_id = uuid("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee");
        let occ = occurrence(uuid(ROW_B));
        let mut full = item(occ.id);
        full.source_span_id = Some(span_id);
        full.quoted_text = Some("canon words".to_owned());
        full.verify_status = Some("supported".to_owned());
        full.edition_key = Some("DABAR_2026".to_owned());
        full.edition_id = Some(edition_id);
        full.locator = Map::from_iter([("page".to_owned(), Value::from(7))]);
        view.blocks[1].citations = vec![BlockCitations {
            occurrence: occ,
            items: vec![full],
        }];
        view.spans.insert(
            span_id,
            SourceSpan {
                id: span_id,
                document_id: uuid(DOC_ID),
                char_start: 5,
                char_end: 12,
                quoted_text: "canon".to_owned(),
                parser: None,
                parser_version: None,
                passage_id: None,
                created_at: Utc::now(),
            },
        );
        let rendered = render_markdown(&view);
        for expected in [
            format!("document_id: {DOC_ID}\n"),
            "char_start: 5\n".to_owned(),
            "char_end: 12\n".to_owned(),
            "quoted_text: canon words\n".to_owned(),
            "verify_status: supported\n".to_owned(),
            "edition_key: DABAR_2026\n".to_owned(),
            format!("edition_id: {edition_id}\n"),
            "  locator:\n    page: 7\n".to_owned(),
        ] {
            assert!(
                rendered.contains(&expected),
                "missing {expected}:\n{rendered}"
            );
        }
    }

    /// Headings carry their body, paragraphs carry titled comments, and
    /// only `BlockEnd` citations append markers.
    #[test]
    fn test_render_block_shapes_and_marker_placement() {
        let mut view = cited_view("T");
        view.blocks[0].block.body_markdown = "Intro body.".to_owned();
        view.blocks[1].block.title = Some("Aside".to_owned());
        let inline = CitationOccurrence {
            placement: Placement::Inline,
            ..occurrence(uuid(ROW_B))
        };
        view.blocks[1].citations = vec![BlockCitations {
            occurrence: inline,
            items: vec![item(uuid("77777777-7777-7777-7777-777777777777"))],
        }];
        let rendered = render_markdown(&view);
        assert!(rendered.contains("## Release\nIntro body.\n"));
        assert!(rendered.contains("<!-- title: Aside -->\n"));
        assert!(
            !rendered.contains(&format_marker(&uuid(CITE_A))),
            "inline citations never append a block-end marker:\n{rendered}"
        );
    }
    /// Front-matter shapes: blank and `~` fronts read as empty mappings, a
    /// sequence front and broken YAML refuse with exact messages.
    #[test]
    fn test_parse_front_matter_shapes() {
        let (front, parsed) = parse_markdown("---\n\n---\n\nText.\n").expect("blank front");
        assert!(front.is_empty());
        assert_eq!(parsed.len(), 1);
        let (front, _) = parse_markdown("---\n~\n---\n\nText.\n").expect("tilde front");
        assert!(front.is_empty());
        let error = parse_markdown("---\n- a\n- b\n---\n").expect_err("sequence front");
        assert!(
            matches!(error, Error::Validation(message) if message == "Front matter must be a mapping")
        );
        let error = parse_markdown("---\nkey: [unclosed\n---\n").expect_err("broken yaml");
        assert!(
            matches!(error, Error::Validation(message) if message.starts_with("Invalid YAML front matter: "))
        );
    }

    /// Non-string YAML keys (bool, int, float, null, sequence, tagged)
    /// arrive stringified with JSON values, mirroring the Python loader.
    /// (serde_yaml keeps `yes` a string under YAML 1.2 where PyYAML 1.1
    /// reads `true`, so the bool case spells `true: true`.)
    #[test]
    fn test_parse_front_matter_key_shapes() {
        let markdown = "---\ntrue: true\n5: five\n1.5: half\nnull: nothing\nplain: ok\nintval: 5\nnegval: -3\nfloatval: 1.5\nkey: !foo bar\n? [1, 2]\n: pair\n---\n\nText.\n";
        let (front, parsed) = parse_markdown(markdown).expect("parse");
        assert_eq!(front.get("true"), Some(&Value::Bool(true)));
        assert_eq!(front.get("5"), Some(&Value::String("five".to_owned())));
        assert_eq!(front.get("1.5"), Some(&Value::String("half".to_owned())));
        assert_eq!(
            front.get("null"),
            Some(&Value::String("nothing".to_owned()))
        );
        assert_eq!(front.get("plain"), Some(&Value::String("ok".to_owned())));
        assert_eq!(front.get("intval"), Some(&Value::from(5)));
        assert_eq!(front.get("negval"), Some(&Value::from(-3)));
        assert_eq!(front.get("floatval"), Some(&Value::from(1.5)));
        assert_eq!(front.get("key"), Some(&Value::String("bar".to_owned())));
        assert_eq!(front.get("[1,2]"), Some(&Value::String("pair".to_owned())));
        assert_eq!(parsed.len(), 1);
    }

    /// Empty titles vanish (title comments and bare headings), and a
    /// shallower heading pops the whole stack so its parent is empty.
    #[test]
    fn test_parse_empty_titles_and_stack_pop() {
        let markdown =
            "---\nwork: deror\n---\n\n<!-- title: -->\nA note.\n\n#   \n\n# A\n\n## B\n\n# C\n";
        let (_, parsed) = parse_markdown(markdown).expect("parse");
        assert_eq!(parsed[0].title, None);
        assert_eq!(parsed[0].body, "A note.");
        assert_eq!(parsed[1].block_type, "heading");
        assert_eq!(parsed[1].title, None);
        let titles: Vec<Option<&str>> = parsed.iter().map(|block| block.title.as_deref()).collect();
        assert_eq!(titles[2], Some("A"));
        assert_eq!(parsed[3].parent_index, Some(2));
        assert_eq!(parsed[4].title.as_deref(), Some("C"));
        assert_eq!(parsed[4].parent_index, None);
    }
    /// `export_draft` renders the assembled current revision through both
    /// selectors, and `export_draft_text` is the same render by id.
    #[test]
    fn test_export_draft_paths() {
        let fakes = Mutex::new(Fakes::rig());
        let by_slug = block_on(service(&fakes).export_draft(Some("deror"), None)).expect("slug");
        let by_id = block_on(service(&fakes).export_draft(None, Some(uuid(WORK_ID)))).expect("id");
        assert_eq!(by_slug, by_id);
        assert!(by_slug.contains("work: deror\n"));
        assert!(by_slug.contains("## Release\n"));
        assert!(by_slug.contains(&format_marker(&uuid(CITE_A))));
        assert!(by_slug.contains("edition_key: DABAR_2026\n"));
        let text = block_on(service(&fakes).export_draft_text(uuid(WORK_ID))).expect("text");
        assert_eq!(text, by_slug);
        // No selector names no work.
        let error = block_on(service(&fakes).export_draft(None, None)).expect_err("none");
        assert!(matches!(error, Error::NotFound { kind: "work", id } if id == "None"));
        // An unknown id misses too.
        let missing = uuid("aaaaaaaa-0000-0000-0000-000000000000");
        let error = block_on(service(&fakes).export_draft(None, Some(missing))).expect_err("id");
        assert!(matches!(error, Error::NotFound { kind: "work", .. }));
    }

    /// A revision row that vanished after the work pinned it refuses with
    /// its id.
    #[test]
    fn test_resolve_current_missing_revision() {
        let fakes = Mutex::new(Fakes::rig());
        let ghost = uuid("bbbbbbbb-0000-0000-0000-000000000000");
        fakes
            .lock()
            .expect("lock")
            .work
            .as_mut()
            .expect("work")
            .current_revision_id = Some(ghost);
        let error =
            block_on(service(&fakes).resolve_current(Some("deror"), None)).expect_err("ghost");
        assert!(matches!(
            error,
            Error::NotFound { kind: "work_revision", id } if id == ghost.to_string()
        ));
    }

    /// Each import failure after `begin` rolls back and reports `Failed`:
    /// copy-forward, apply, and promote errors alike.
    #[test]
    fn test_import_error_paths_roll_back() {
        for (flag, message) in [
            ("copy_forward", "copy_forward injected"),
            ("upsert", "upsert injected"),
            ("set_current", "set_current injected"),
        ] {
            let fakes = Mutex::new(Fakes::rig());
            {
                let mut guard = fakes.lock().expect("lock");
                match flag {
                    "copy_forward" => guard.fail_copy_forward = true,
                    "upsert" => guard.fail_upsert = true,
                    _ => guard.fail_set_current = true,
                }
            }
            let exported = fakes.lock().expect("lock").markdown();
            // An edited paragraph forces a real row write: the unedited
            // export is a no-op diff that never reaches `upsert`.
            let edited = exported.replace("The prophets speak.", "The prophets speak twice.");
            let error = block_on(service(&fakes).import_draft(Some("deror"), None, &edited, false))
                .expect_err(flag);
            assert_eq!(
                format!("{error}"),
                format!("database or storage error: {message}"),
                "{flag} must fail, not refuse"
            );
            assert_eq!(
                fakes.lock().expect("lock").tx_events,
                vec!["begin", "rollback"]
            );
        }
    }

    /// Storage failures at each fallible lookup surface as `Failed` with
    /// the repository message attached: export assembly, import inventory,
    /// and both `resolve_current` selectors.
    #[test]
    fn test_service_lookup_errors_surface_as_failed() {
        let fakes = Mutex::new(Fakes::rig());
        fakes.lock().expect("lock").fail_for_revision = true;
        let error = block_on(service(&fakes).export_draft(Some("deror"), None)).expect_err("list");
        assert_eq!(
            format!("{error}"),
            "database or storage error: for_revision injected"
        );
        for (flag, message) in [
            ("tree", "tree injected"),
            ("for_revision", "for_revision injected"),
        ] {
            let fakes = Mutex::new(Fakes::rig());
            {
                let mut guard = fakes.lock().expect("lock");
                match flag {
                    "tree" => guard.fail_tree = true,
                    _ => guard.fail_for_revision = true,
                }
            }
            let exported = fakes.lock().expect("lock").markdown();
            let error =
                block_on(service(&fakes).import_draft(Some("deror"), None, &exported, false))
                    .expect_err(flag);
            assert_eq!(
                format!("{error}"),
                format!("database or storage error: {message}"),
                "{flag} must fail"
            );
            // Inventory runs before `begin`, so no transaction event fires.
            assert!(fakes.lock().expect("lock").tx_events.is_empty());
        }
        let fakes = Mutex::new(Fakes::rig());
        fakes.lock().expect("lock").fail_get_by_slug = true;
        let error = block_on(service(&fakes).export_draft(Some("deror"), None)).expect_err("slug");
        assert_eq!(
            format!("{error}"),
            "database or storage error: get_by_slug injected"
        );
        let fakes = Mutex::new(Fakes::rig());
        fakes.lock().expect("lock").fail_get_work = true;
        let missing = uuid("aaaaaaaa-0000-0000-0000-000000000000");
        let error = block_on(service(&fakes).export_draft(None, Some(missing))).expect_err("id");
        assert_eq!(
            format!("{error}"),
            "database or storage error: get_work injected"
        );
        let fakes = Mutex::new(Fakes::rig());
        fakes.lock().expect("lock").fail_rev_get = true;
        let error = block_on(service(&fakes).export_draft(Some("deror"), None)).expect_err("rev");
        assert_eq!(
            format!("{error}"),
            "database or storage error: rev_get injected"
        );
    }

    /// `begin` failure reports before any event; `commit` failure propagates
    /// without a rollback, pinning the import tail contract.
    #[test]
    fn test_import_begin_and_commit_errors() {
        let fakes = Mutex::new(Fakes::rig());
        fakes.lock().expect("lock").fail_begin = true;
        let exported = fakes.lock().expect("lock").markdown();
        let error = block_on(service(&fakes).import_draft(Some("deror"), None, &exported, false))
            .expect_err("begin");
        assert_eq!(
            format!("{error}"),
            "database or storage error: begin injected"
        );
        assert!(fakes.lock().expect("lock").tx_events.is_empty());
        let fakes = Mutex::new(Fakes::rig());
        fakes.lock().expect("lock").fail_commit = true;
        let exported = fakes.lock().expect("lock").markdown();
        let error = block_on(service(&fakes).import_draft(Some("deror"), None, &exported, false))
            .expect_err("commit");
        assert_eq!(
            format!("{error}"),
            "database or storage error: commit injected"
        );
        assert_eq!(fakes.lock().expect("lock").tx_events, vec!["begin"]);
    }

    /// Markdown without front matter never reaches the repositories: the
    /// parse refusal reports `Failed` with the exact message.
    #[test]
    fn test_import_without_front_matter_fails_parse() {
        let fakes = Mutex::new(Fakes::rig());
        let error =
            block_on(service(&fakes).import_draft(Some("deror"), None, "just a paragraph", false))
                .expect_err("parse");
        assert_eq!(
            format!("{error}"),
            "data validation failed: Import needs YAML front matter between --- lines"
        );
        assert!(fakes.lock().expect("lock").tx_events.is_empty());
    }

    /// Misordered parents and absent parents refuse out of `apply_blocks`
    /// with exact messages.
    #[test]
    fn test_apply_parent_errors() {
        let fakes = Mutex::new(Fakes::rig());
        let shared = Shared(&fakes);
        let mut tx = block_on(shared.begin()).expect("begin");
        let forward = vec![ParsedBlock {
            key: Some(uuid(KEY_A)),
            block_type: "paragraph".to_owned(),
            title: None,
            body: "Orphan.".to_owned(),
            level: 2,
            parent_index: Some(1),
        }];
        let error = block_on(service(&fakes).apply_blocks(&mut tx, uuid(REV_ID), &forward, &[]))
            .expect_err("forward parent");
        assert!(
            matches!(error, Error::Validation(message) if message == "A block's parent must precede it in the file")
        );
        fakes.lock().expect("lock").hidden_keys.push(uuid(KEY_A));
        let nested = vec![
            ParsedBlock {
                key: Some(uuid(KEY_A)),
                block_type: "heading".to_owned(),
                title: Some("P".to_owned()),
                body: String::new(),
                level: 1,
                parent_index: None,
            },
            ParsedBlock {
                key: Some(uuid(KEY_B)),
                block_type: "paragraph".to_owned(),
                title: None,
                body: "Child.".to_owned(),
                level: 2,
                parent_index: Some(0),
            },
        ];
        let error = block_on(service(&fakes).apply_blocks(&mut tx, uuid(REV_ID), &nested, &[]))
            .expect_err("absent parent");
        assert!(
            matches!(error, Error::Validation(message) if message == format!("Parent block {} is not in this revision", KEY_A))
        );
    }

    /// Row-lookup and row-delete storage failures surface out of
    /// `apply_blocks` with the repository message: the parent lookup fails
    /// on the second row read, the own-row lookup on the first, and the
    /// deletion lookup with no parsed rows at all.
    #[test]
    fn test_apply_block_storage_errors() {
        let parented = || {
            vec![
                ParsedBlock {
                    key: Some(uuid(KEY_A)),
                    block_type: "paragraph".to_owned(),
                    title: None,
                    body: "Parent.".to_owned(),
                    level: 2,
                    parent_index: None,
                },
                ParsedBlock {
                    key: Some(uuid(KEY_B)),
                    block_type: "paragraph".to_owned(),
                    title: None,
                    body: "Child.".to_owned(),
                    level: 2,
                    parent_index: Some(0),
                },
            ]
        };
        let fakes = Mutex::new(Fakes::rig());
        fakes.lock().expect("lock").fail_by_key_on_call = Some(2);
        let shared = Shared(&fakes);
        let mut tx = block_on(shared.begin()).expect("begin");
        let error = block_on(service(&fakes).apply_blocks(&mut tx, uuid(REV_ID), &parented(), &[]))
            .expect_err("parent lookup");
        assert_eq!(
            format!("{error}"),
            "database or storage error: by_key_in_tx injected"
        );
        let fakes = Mutex::new(Fakes::rig());
        fakes.lock().expect("lock").fail_by_key_on_call = Some(1);
        let shared = Shared(&fakes);
        let mut tx = block_on(shared.begin()).expect("begin");
        let error =
            block_on(service(&fakes).apply_blocks(&mut tx, uuid(REV_ID), &parented()[..1], &[]))
                .expect_err("own lookup");
        assert_eq!(
            format!("{error}"),
            "database or storage error: by_key_in_tx injected"
        );
        let fakes = Mutex::new(Fakes::rig());
        fakes.lock().expect("lock").seed_rows();
        fakes.lock().expect("lock").fail_by_key_on_call = Some(1);
        let shared = Shared(&fakes);
        let mut tx = block_on(shared.begin()).expect("begin");
        let error = block_on(service(&fakes).apply_blocks(
            &mut tx,
            uuid(REV_ID),
            &[],
            &[(KEY_A.to_owned(), 0)],
        ))
        .expect_err("deletion lookup");
        assert_eq!(
            format!("{error}"),
            "database or storage error: by_key_in_tx injected"
        );
        let fakes = Mutex::new(Fakes::rig());
        fakes.lock().expect("lock").seed_rows();
        fakes.lock().expect("lock").fail_delete = true;
        let shared = Shared(&fakes);
        let mut tx = block_on(shared.begin()).expect("begin");
        let error = block_on(service(&fakes).apply_blocks(
            &mut tx,
            uuid(REV_ID),
            &[],
            &[(KEY_A.to_owned(), 0)],
        ))
        .expect_err("delete");
        assert_eq!(
            format!("{error}"),
            "database or storage error: delete injected"
        );
    }

    /// Same-depth deletions keep tree order even when key order disagrees:
    /// the stable deepest-first sort must not promote key order.
    #[test]
    fn test_apply_deletion_ties_keep_tree_order() {
        const KEY_ZERO: &str = "00000000-0000-0000-0000-000000000000";
        const ROW_ZERO: &str = "00000000-1111-1111-1111-000000000000";
        let fakes = Mutex::new(Fakes::rig());
        {
            let mut guard = fakes.lock().expect("lock");
            guard.tree.push(row(RowParams {
                id: ROW_ZERO,
                key: KEY_ZERO,
                parent_id: None,
                position: 1,
                block_type: "paragraph",
                title: None,
                body: "Extra root.",
                attributes: Map::new(),
            }));
            guard.seed_rows();
        }
        let shared = Shared(&fakes);
        let mut tx = block_on(shared.begin()).expect("begin");
        // Tree order is `[KEY_A, KEY_ZERO]` (both depth 0) while key order
        // is the reverse; the diff must follow the tree.
        let changes = block_on(service(&fakes).apply_blocks(
            &mut tx,
            uuid(REV_ID),
            &[],
            &[(KEY_A.to_owned(), 0), (KEY_ZERO.to_owned(), 0)],
        ))
        .expect("deletions");
        let order: Vec<(&str, &str)> = changes
            .iter()
            .map(|change| (change.block_key.as_str(), change.change.as_str()))
            .collect();
        assert_eq!(order, vec![(KEY_A, "deleted"), (KEY_ZERO, "deleted")]);
    }

    /// An added block takes the insert `upsert` path: failing it reports
    /// `Failed` and rolls back, covering the insert arm the edited-only
    /// rollback test never reaches.
    #[test]
    fn test_import_insert_upsert_error_rolls_back() {
        let fakes = Mutex::new(Fakes::rig());
        fakes.lock().expect("lock").fail_upsert = true;
        let exported = fakes.lock().expect("lock").markdown();
        let edited = exported.replace(
            "{{cite:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}}.\n",
            "{{cite:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}}.\n\nA new claim.\n",
        );
        let error = block_on(service(&fakes).import_draft(Some("deror"), None, &edited, false))
            .expect_err("insert upsert");
        assert_eq!(
            format!("{error}"),
            "database or storage error: upsert injected"
        );
        assert_eq!(
            fakes.lock().expect("lock").tx_events,
            vec!["begin", "rollback"]
        );
    }

    /// Updating without a title leaves the row title alone: the
    /// title-carrying arm is skipped, not failed.
    #[test]
    fn test_shared_update_without_title_keeps_title() {
        let fakes = Mutex::new(Fakes::rig());
        let shared = Shared(&fakes);
        let mut tx = block_on(shared.begin()).expect("begin");
        let before = fakes
            .lock()
            .expect("lock")
            .work
            .clone()
            .expect("work")
            .title;
        let updated = block_on(shared.update(&mut tx, uuid(WORK_ID), Utc::now(), Map::new()))
            .expect("update");
        assert_eq!(updated.title, before);
    }

    /// A brand-new heading stores its level, and deletions run deepest
    /// first; unknown depth keys simply vanish.
    #[test]
    fn test_apply_heading_insert_and_deletion_order() {
        let fakes = Mutex::new(Fakes::rig());
        let exported = fakes.lock().expect("lock").markdown();
        let edited = format!("{exported}\n# Added\n");
        let diff = block_on(service(&fakes).import_draft(Some("deror"), None, &edited, false))
            .expect("import");
        assert_eq!(diff.changes.len(), 1);
        assert_eq!(diff.changes[0].change, "added");
        let guard = fakes.lock().expect("lock");
        let (draft, expected) = guard
            .upserts
            .iter()
            .find(|(draft, _)| draft.block_type == "heading")
            .expect("heading upsert");
        assert_eq!(expected, &None);
        assert_eq!(draft.attributes.get("level"), Some(&Value::from(1)));
        drop(guard);
        // Dropping both tree blocks deletes the child before the parent.
        let fakes = Mutex::new(Fakes::rig());
        let diff = block_on(service(&fakes).import_draft(
            Some("deror"),
            None,
            "---\nwork: deror\n---\n",
            false,
        ))
        .expect("import");
        let order: Vec<(&str, &str)> = diff
            .changes
            .iter()
            .map(|change| (change.block_key.as_str(), change.change.as_str()))
            .collect();
        assert_eq!(order, vec![(KEY_B, "deleted"), (KEY_A, "deleted")]);
        // A depth entry with no row behind it is a silent skip.
        let fakes = Mutex::new(Fakes::rig());
        let shared = Shared(&fakes);
        let mut tx = block_on(shared.begin()).expect("begin");
        let changes = block_on(service(&fakes).apply_blocks(
            &mut tx,
            uuid(REV_ID),
            &[],
            &[("aaaaaaaa-aaaa-aaaa-aaaa-000000000099".to_owned(), 2)],
        ))
        .expect("skip");
        assert!(changes.is_empty());
    }
    /// `py_repr_json` over every front-work shape, through the wrong-work
    /// refusal that reports it.
    #[test]
    fn test_import_wrong_work_repr_shapes() {
        for (front, expected) in [
            ("true", "True"),
            ("false", "False"),
            ("5", "5"),
            ("null", "None"),
            ("[1, two]", "[1, 'two']"),
            ("{a: 1}", "{'a': 1}"),
            ("{a: [1, {b: null}]}", "{'a': [1, {'b': None}]}"),
        ] {
            let fakes = Mutex::new(Fakes::rig());
            let exported = fakes.lock().expect("lock").markdown();
            let renamed = exported.replace("work: deror", &format!("work: {front}"));
            let error =
                block_on(service(&fakes).import_draft(Some("deror"), None, &renamed, false))
                    .expect_err("must fail");
            assert!(
                matches!(
                    error,
                    ImportError::Failed(Error::Validation(message))
                        if message == format!("Import names work {expected}, not 'deror'")
                ),
                "front {front}"
            );
            assert!(fakes.lock().expect("lock").tx_events.is_empty());
        }
        // A front with no work key at all reports `None`.
        let fakes = Mutex::new(Fakes::rig());
        let exported = fakes.lock().expect("lock").markdown();
        let stripped = exported.replace("work: deror\n", "");
        let error = block_on(service(&fakes).import_draft(Some("deror"), None, &stripped, false))
            .expect_err("must fail");
        assert!(matches!(
            error,
            ImportError::Failed(Error::Validation(message))
                if message == "Import names work None, not 'deror'"
        ));
    }

    /// `block_on` drives a future that pends once: the waker clone, wake,
    /// drop, and the pending arm all run.
    #[test]
    fn test_block_on_drives_pending_future() {
        struct PendOnce {
            polled: bool,
        }
        impl Future for PendOnce {
            type Output = i32;
            fn poll(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<i32> {
                if self.polled {
                    Poll::Ready(7)
                } else {
                    self.polled = true;
                    context.waker().wake_by_ref();
                    Poll::Pending
                }
            }
        }
        assert_eq!(block_on(PendOnce { polled: false }), 7);
    }
    /// One pass over the whole ports surface: every fake method runs once
    /// against real in-memory state, and each call asserts what it stored
    /// or returned.
    #[test]
    fn test_ports_surface_fake_behaviors() {
        use marginalia_types::ports::{
            CitationRepo as _, SourceSpanRepo as _, WorkBlockRepo as _, WorkLinkRepo as _,
            WorkRepo as _, WorkRevisionRepo as _,
        };
        use marginalia_types::works_ports::TxFactory as _;
        let fakes = Mutex::new(Fakes::rig());
        let shared = Shared(&fakes);
        // TxFactory: begin/commit/rollback record their events.
        let mut tx = block_on(shared.begin()).expect("begin");
        // WorkRepo::insert stores; get/get_by_slug/list return the row.
        let inserted = block_on(WorkRepo::insert(
            &shared,
            &mut tx,
            WorkDraft {
                slug: "second".to_owned(),
                title: "Second".to_owned(),
                work_type: "essay".to_owned(),
                language: None,
                abstract_text: None,
                metadata: Map::new(),
            },
        ))
        .expect("insert");
        assert_eq!(
            block_on(WorkRepo::get(&shared, inserted.id)).expect("get"),
            Some(inserted.clone())
        );
        assert_eq!(
            block_on(shared.get_by_slug("second")).expect("slug"),
            Some(inserted.clone())
        );
        assert_eq!(block_on(shared.list()).expect("list").len(), 2);
        // WorkRepo::update applies fields; archive flips the status.
        let updated = block_on(shared.update(
            &mut tx,
            inserted.id,
            inserted.updated_at,
            Map::from_iter([("title".to_owned(), Value::String("Renamed".to_owned()))]),
        ))
        .expect("update");
        assert_eq!(updated.title, "Renamed");
        let archived = block_on(shared.archive(&mut tx, inserted.id)).expect("archive");
        assert_eq!(archived.status, WorkStatus::Archived);
        assert!(archived.archived_at.is_some());
        let pinned = uuid("aaaaaaaa-0000-0000-0000-000000000000");
        block_on(shared.set_current_revision(&mut tx, inserted.id, pinned)).expect("set current");
        assert_eq!(
            block_on(WorkRepo::get(&shared, inserted.id))
                .expect("get")
                .expect("row")
                .current_revision_id,
            Some(pinned)
        );
        // WorkRevisionRepo::insert stores; get/latest find the row.
        let draft_revision = block_on(WorkRevisionRepo::insert(
            &shared,
            &mut tx,
            WorkRevisionDraft {
                work_id: inserted.id,
                revision_number: 1,
                parent_revision_id: None,
                message: Some("first".to_owned()),
                created_by: "user".to_owned(),
                metadata: Map::new(),
            },
        ))
        .expect("insert revision");
        assert_eq!(
            block_on(WorkRevisionRepo::get(&shared, draft_revision.id)).expect("get"),
            Some(draft_revision.clone())
        );
        assert_eq!(
            block_on(shared.latest(inserted.id))
                .expect("latest")
                .expect("row")
                .id,
            draft_revision.id
        );
        // set_message/freeze/publish/supersede walk one revision forward.
        let noted =
            block_on(shared.set_message(&mut tx, draft_revision.id, "note")).expect("message");
        assert_eq!(noted.message.as_deref(), Some("note"));
        let frozen = block_on(shared.freeze(&mut tx, draft_revision.id, b"hash")).expect("freeze");
        assert_eq!(frozen.state, RevisionState::Frozen);
        assert_eq!(frozen.content_hash, Some(b"hash".to_vec()));
        let published = block_on(shared.publish(&mut tx, draft_revision.id)).expect("publish");
        assert_eq!(published.state, RevisionState::Published);
        let old = block_on(shared.supersede(&mut tx, draft_revision.id)).expect("supersede");
        assert_eq!(old.state, RevisionState::Superseded);
        // WorkBlockRepo: reads find seeded rows, upsert stores, delete drops.
        block_on(shared.copy_forward(&mut tx, uuid(REV_ID))).expect("seed");
        let created_id = fakes.lock().expect("lock").created.id;
        let keyed = block_on(WorkBlockRepo::by_key(&shared, created_id, uuid(KEY_A)))
            .expect("by_key")
            .expect("row");
        assert_eq!(keyed.block_key, uuid(KEY_A));
        assert_eq!(
            block_on(shared.by_key_in_tx(&mut tx, created_id, uuid(KEY_A)))
                .expect("in-tx")
                .expect("row")
                .id,
            keyed.id
        );
        assert_eq!(
            block_on(WorkBlockRepo::get(&shared, keyed.id))
                .expect("get")
                .expect("row")
                .id,
            keyed.id
        );
        let draft_block = WorkBlockDraft {
            revision_id: created_id,
            block_key: uuid("99999999-1111-2222-3333-444444444444"),
            parent_id: None,
            position: 9,
            block_type: "paragraph".to_owned(),
            title: None,
            body_markdown: "New.".to_owned(),
            attributes: Map::new(),
        };
        let stored =
            block_on(shared.upsert(&mut tx, created_id, draft_block, None)).expect("upsert");
        assert_eq!(stored.body_markdown, "New.");
        block_on(shared.delete(&mut tx, stored.id)).expect("delete");
        assert!(
            block_on(WorkBlockRepo::get(&shared, stored.id))
                .expect("get")
                .is_none(),
            "delete removes the row"
        );
        // CitationRepo: inserts store; lookups filter the rigged entries.
        let occ = block_on(shared.insert_occurrence(
            &mut tx,
            OccurrenceDraft {
                block_id: uuid(ROW_B),
                citation_key: uuid("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                placement: Placement::Inline,
                intent: Intent::Support,
                note: None,
            },
        ))
        .expect("occurrence");
        assert_eq!(occ.intent, Intent::Support);
        let cited_item = block_on(shared.insert_item(
            &mut tx,
            CitationItemDraft {
                occurrence_id: occ.id,
                position: 0,
                edition_id: None,
                edition_key: Some("K".to_owned()),
                source_span_id: None,
                quoted_text: None,
                verify_status: None,
                locator: Map::new(),
                prefix: None,
                suffix: None,
                suppress_author: false,
            },
        ))
        .expect("item");
        assert_eq!(cited_item.edition_key.as_deref(), Some("K"));
        assert_eq!(
            block_on(CitationRepo::for_block(&shared, uuid(ROW_B)))
                .expect("for_block")
                .len(),
            1
        );
        assert_eq!(
            block_on(CitationRepo::by_key(&shared, uuid(REV_ID), uuid(CITE_A)))
                .expect("by_key")
                .expect("entry")
                .occurrence
                .citation_key,
            uuid(CITE_A)
        );
        assert_eq!(
            block_on(shared.citing_key("DABAR_2026"))
                .expect("citing")
                .len(),
            1
        );
        assert!(block_on(shared.citing_span(uuid(DOC_ID)))
            .expect("span")
            .is_empty());
        // WorkLinkRepo: adds store; lookups filter by owner.
        let span_id = uuid("99999999-9999-9999-9999-999999999999");
        let entity_id = uuid("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee");
        block_on(shared.add_source_link(
            &mut tx,
            BlockSourceLinkDraft {
                block_id: uuid(ROW_B),
                source_span_id: span_id,
                relation: "quotes".to_owned(),
                confidence: None,
                note: None,
            },
        ))
        .expect("source link");
        block_on(shared.add_entity_link(
            &mut tx,
            BlockEntityLinkDraft {
                block_id: uuid(ROW_B),
                entity_id,
                relation: "about".to_owned(),
                surface_form: None,
            },
        ))
        .expect("entity link");
        assert_eq!(
            block_on(WorkLinkRepo::for_block(&shared, uuid(ROW_B)))
                .expect("links")
                .sources
                .len(),
            1
        );
        assert_eq!(
            block_on(shared.for_span(span_id))
                .expect("span links")
                .len(),
            1
        );
        assert_eq!(
            block_on(shared.for_entity(entity_id, "about"))
                .expect("entity links")
                .len(),
            1
        );
        assert!(block_on(shared.for_entity(entity_id, "other"))
            .expect("relation filter")
            .is_empty());
        // SourceSpanRepo: resolve stores; get/for_document/stale read back.
        let span = block_on(shared.resolve(&mut tx, uuid(DOC_ID), 1, 4)).expect("resolve");
        assert_eq!(
            block_on(SourceSpanRepo::get(&shared, span.id))
                .expect("get")
                .expect("row")
                .char_end,
            4
        );
        assert_eq!(
            block_on(shared.for_document(uuid(DOC_ID)))
                .expect("doc")
                .len(),
            1
        );
        assert_eq!(block_on(shared.stale(10)).expect("stale").len(), 1);
        assert!(block_on(shared.stale(0)).expect("stale zero").is_empty());
        block_on(shared.commit(tx)).expect("commit");
        assert_eq!(
            fakes.lock().expect("lock").tx_events,
            vec!["begin", "commit"]
        );
    }
    /// The fake's secondary branches: rig-row writes, carried-revision
    /// lifecycle, unknown-id refusals, and the tree fallback for reads.
    #[test]
    fn test_fake_secondary_branches() {
        use marginalia_types::ports::{WorkBlockRepo as _, WorkRepo as _, WorkRevisionRepo as _};
        use marginalia_types::works_ports::TxFactory as _;
        let fakes = Mutex::new(Fakes::rig());
        let shared = Shared(&fakes);
        let mut tx = block_on(shared.begin()).expect("begin");
        let created_id = fakes.lock().expect("lock").created.id;
        block_on(shared.set_current_revision(&mut tx, uuid(WORK_ID), created_id)).expect("set");
        assert_eq!(
            fakes
                .lock()
                .expect("lock")
                .work
                .as_ref()
                .expect("work")
                .current_revision_id,
            Some(created_id)
        );
        let renamed = block_on(shared.update(
            &mut tx,
            uuid(WORK_ID),
            Utc::now(),
            Map::from_iter([("title".to_owned(), Value::String("R".to_owned()))]),
        ))
        .expect("update");
        assert_eq!(renamed.title, "R");
        let ghost = uuid("aaaaaaaa-0000-0000-0000-000000000000");
        assert!(matches!(
            block_on(shared.update(&mut tx, ghost, Utc::now(), Map::new())).expect_err("ghost"),
            Error::NotFound { kind: "work", .. }
        ));
        let archived = block_on(shared.archive(&mut tx, uuid(WORK_ID))).expect("archive");
        assert_eq!(archived.status, WorkStatus::Archived);
        assert!(matches!(
            block_on(shared.archive(&mut tx, ghost)).expect_err("ghost"),
            Error::NotFound { kind: "work", .. }
        ));
        assert_eq!(
            block_on(WorkRevisionRepo::get(&shared, created_id))
                .expect("get")
                .expect("row")
                .id,
            created_id
        );
        let noted = block_on(shared.set_message(&mut tx, created_id, "m")).expect("message");
        assert_eq!(noted.message.as_deref(), Some("m"));
        let frozen = block_on(shared.freeze(&mut tx, created_id, b"h")).expect("freeze");
        assert_eq!(frozen.state, RevisionState::Frozen);
        let published = block_on(shared.publish(&mut tx, created_id)).expect("publish");
        assert_eq!(published.state, RevisionState::Published);
        let old = block_on(shared.supersede(&mut tx, created_id)).expect("supersede");
        assert_eq!(old.state, RevisionState::Superseded);
        for (label, result) in [
            (
                "message",
                block_on(shared.set_message(&mut tx, ghost, "m")).map(|_| ()),
            ),
            (
                "freeze",
                block_on(shared.freeze(&mut tx, ghost, b"h")).map(|_| ()),
            ),
            (
                "publish",
                block_on(shared.publish(&mut tx, ghost)).map(|_| ()),
            ),
            (
                "supersede",
                block_on(shared.supersede(&mut tx, ghost)).map(|_| ()),
            ),
        ] {
            assert!(
                matches!(
                    result.expect_err(label),
                    Error::NotFound {
                        kind: "work_revision",
                        ..
                    }
                ),
                "{label} refuses unknown revisions"
            );
        }
        block_on(shared.copy_forward(&mut tx, uuid(REV_ID))).expect("seed");
        let keyed = block_on(WorkBlockRepo::by_key(&shared, created_id, uuid(KEY_A)))
            .expect("by_key")
            .expect("row");
        block_on(shared.delete(&mut tx, keyed.id)).expect("delete");
        let fell = block_on(WorkBlockRepo::by_key(&shared, created_id, uuid(KEY_A)))
            .expect("by_key")
            .expect("tree row");
        assert_eq!(fell.block_type, "heading");
        assert_eq!(fell.title.as_deref(), Some("Release"));
        block_on(shared.rollback(tx)).expect("rollback");
    }
    /// Every `fail_*` flag makes exactly its method report a storage error
    /// (what the import service turns into `Failed` + rollback); the flag
    /// name rides into the message so a failure names its source.
    #[test]
    fn test_fake_failure_injections_report_storage_errors() {
        use marginalia_types::ports::{CitationRepo as _, WorkBlockRepo as _, WorkRepo as _};
        use marginalia_types::works_ports::TxFactory as _;
        for (flag, message) in [
            ("begin", "begin injected"),
            ("commit", "commit injected"),
            ("get_work", "get_work injected"),
            ("get_by_slug", "get_by_slug injected"),
            ("rev_get", "rev_get injected"),
            ("tree", "tree injected"),
            ("by_key", "by_key_in_tx injected"),
            ("delete", "delete injected"),
            ("for_revision", "for_revision injected"),
        ] {
            let fakes = Mutex::new(Fakes::rig());
            let shared = Shared(&fakes);
            let mut tx = block_on(shared.begin()).expect("begin");
            let error = match flag {
                "begin" => {
                    fakes.lock().expect("lock").fail_begin = true;
                    block_on(shared.begin()).map(|_| ()).expect_err(flag)
                }
                "commit" => {
                    fakes.lock().expect("lock").fail_commit = true;
                    block_on(shared.commit(tx)).map(|_| ()).expect_err(flag)
                }
                "get_work" => {
                    fakes.lock().expect("lock").fail_get_work = true;
                    block_on(WorkRepo::get(&shared, uuid(WORK_ID)))
                        .map(|_| ())
                        .expect_err(flag)
                }
                "get_by_slug" => {
                    fakes.lock().expect("lock").fail_get_by_slug = true;
                    block_on(shared.get_by_slug("deror"))
                        .map(|_| ())
                        .expect_err(flag)
                }
                "rev_get" => {
                    fakes.lock().expect("lock").fail_rev_get = true;
                    block_on(WorkRevisionRepo::get(&shared, uuid(REV_ID)))
                        .map(|_| ())
                        .expect_err(flag)
                }
                "tree" => {
                    fakes.lock().expect("lock").fail_tree = true;
                    block_on(shared.tree(uuid(REV_ID)))
                        .map(|_| ())
                        .expect_err(flag)
                }
                "by_key" => {
                    fakes.lock().expect("lock").fail_by_key_on_call = Some(1);
                    block_on(shared.by_key_in_tx(&mut tx, uuid(REV_ID), uuid(KEY_A)))
                        .map(|_| ())
                        .expect_err(flag)
                }
                "delete" => {
                    fakes.lock().expect("lock").fail_delete = true;
                    block_on(shared.delete(&mut tx, uuid(ROW_A)))
                        .map(|_| ())
                        .expect_err(flag)
                }
                _ => {
                    fakes.lock().expect("lock").fail_for_revision = true;
                    block_on(shared.for_revision(uuid(REV_ID)))
                        .map(|_| ())
                        .expect_err(flag)
                }
            };
            assert!(
                matches!(error, Error::Storage(detail) if detail == message),
                "{flag} must report its injection"
            );
        }
    }
}
