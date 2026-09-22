//! Entity reconciliation, part 1: driver and markup scanner.

use marginalia_types::{Error, Result};

use crate::html_entities::{html5_entity, html_entity};

/// How `<![CDATA[` sections read: dropped with the comments on every
/// backend — the HTML module's exact-type walk never sees them, and EPUB
/// chapters pass through libxml2's rebuild, which parses them as bogus
/// comments too.
/// Rewrite `source` so html5ever decodes its entities exactly the way
/// `html.parser` does. Returns [`Error::Parse`] where `html.parser`
/// raises (`ValueError` on malformed numeric references).
pub fn normalize_entities(source: &str) -> Result<String, Error> {
    let bytes = source.as_bytes();
    let len = bytes.len();
    let mut out = String::with_capacity(source.len());
    let mut i = 0;
    while i < len {
        match bytes[i] {
            b'&' => {
                let (replacement, next) = text_reference(source, i)?;
                out.push_str(&replacement);
                i = next;
            }
            b'<' => {
                i = copy_markup(source, bytes, i, &mut out)?;
            }
            _ => {
                let ch = source[i..].chars().next().unwrap_or('\u{FFFD}');
                out.push(ch);
                i += ch.len_utf8();
            }
        }
    }
    Ok(out)
}

/// Copy one markup construct starting at `i` (which holds `<`), processing
/// attribute values and entering raw bodies. Returns the next position.
fn copy_markup(source: &str, bytes: &[u8], i: usize, out: &mut String) -> Result<usize, Error> {
    let len = bytes.len();
    if starts_with_ignore_case(bytes, i, "<!--") {
        let end = find_bytes(bytes, i + 4, b"-->");
        out.push_str(&source[i..end]);
        return Ok(end);
    }
    if starts_with_ignore_case(bytes, i, "<![CDATA[") {
        // A CDATA section ends at `]]>`, or runs on past a missing one;
        // either way its content never reaches the text.
        let end = find_bytes(bytes, i + 9, b"]]>");
        return Ok(end);
    }
    if starts_with_ignore_case(bytes, i, "<!") {
        // Declarations end at the first `>` upstream, quotes
        // notwithstanding — verified against `<!DOCTYPE a "x>y" z>`.
        let end = find_byte(bytes, i + 2, b'>');
        let end = end + usize::from(end < len);
        out.push_str(&source[i..end]);
        return Ok(end);
    }
    if starts_with_ignore_case(bytes, i, "<?") || starts_with_ignore_case(bytes, i, "</") {
        // Processing instructions and close tags end at the first `>`,
        // quotes notwithstanding — exactly the upstream scan.
        let end = find_byte(bytes, i + 2, b'>');
        let end = end + usize::from(end < len);
        out.push_str(&source[i..end]);
        return Ok(end);
    }
    if i + 1 < len && bytes[i + 1].is_ascii_alphabetic() {
        return copy_tag(source, bytes, i, out);
    }
    // `<` followed by anything else is literal text on both sides.
    out.push('<');
    Ok(i + 1)
}

fn starts_with_ignore_case(bytes: &[u8], at: usize, pat: &str) -> bool {
    bytes.len() >= at + pat.len() && bytes[at..at + pat.len()].eq_ignore_ascii_case(pat.as_bytes())
}

fn find_bytes(bytes: &[u8], mut i: usize, pat: &[u8]) -> usize {
    while i + pat.len() <= bytes.len() {
        if &bytes[i..i + pat.len()] == pat {
            return i + pat.len();
        }
        i += 1;
    }
    bytes.len()
}

fn find_byte(bytes: &[u8], mut i: usize, target: u8) -> usize {
    while i < bytes.len() && bytes[i] != target {
        i += 1;
    }
    i
}

/// The end of a raw-text body: `</name` with only whitespace before its
/// `>`. Anything else (`</namex>`, an unterminated `</name`) keeps the body
/// open, exactly as upstream keeps it.
fn close_tag_end(bytes: &[u8], source: &str, i: usize, close: &str) -> Option<usize> {
    if !starts_with_ignore_case(bytes, i, close) {
        return None;
    }
    let mut j = i + close.len();
    while j < bytes.len() && source[j..].starts_with([' ', '\t', '\n', '\x0C', '\r']) {
        j += 1;
    }
    if bytes.get(j) == Some(&b'>') {
        Some(j + 1)
    } else {
        None
    }
}

/// Skip a declaration: quoted `>` characters do not end one.
fn skip_decl(bytes: &[u8], mut k: usize) -> usize {
    while k < bytes.len() {
        match bytes[k] {
            b'"' | b'\'' => {
                k = find_byte(bytes, k + 1, bytes[k]);
                k += usize::from(k < bytes.len());
            }
            b'>' => return k + 1,
            _ => k += 1,
        }
    }
    k
}

/// Copy an open tag, rewriting attribute values under the attribute rules,
/// and enter a raw body when one opens.
fn copy_tag(source: &str, bytes: &[u8], i: usize, out: &mut String) -> Result<usize, Error> {
    let len = bytes.len();
    let mut j = i + 1;
    while j < len && bytes[j].is_ascii_alphanumeric() {
        j += 1;
    }
    let name = source[i + 1..j].to_lowercase();
    out.push_str(&source[i..j]);
    let mut k = j;
    loop {
        while k < len && bytes[k].is_ascii_whitespace() {
            out.push(bytes[k] as char);
            k += 1;
        }
        if k >= len {
            return Ok(k);
        }
        if bytes[k] == b'>' {
            out.push('>');
            k += 1;
            break;
        }
        if bytes[k] == b'/' && bytes.get(k + 1) == Some(&b'>') {
            out.push_str("/>");
            k += 2;
            break;
        }
        // An attribute name (possibly empty grit, emitted literally).
        let start = k;
        while k < len && !matches!(bytes[k], b'=' | b'>' | b'/') && !bytes[k].is_ascii_whitespace()
        {
            k += 1;
        }
        if k == start {
            let ch = source[k..].chars().next().unwrap_or('\u{FFFD}');
            out.push(ch);
            k += ch.len_utf8();
            continue;
        }
        out.push_str(&source[start..k]);
        while k < len && bytes[k].is_ascii_whitespace() {
            k += 1;
        }
        if k < len && bytes[k] == b'=' {
            // Tag-internal whitespace never reaches either DOM.
            out.push('=');
            k += 1;
            while k < len && bytes[k].is_ascii_whitespace() {
                k += 1;
            }
            if k < len && (bytes[k] == b'"' || bytes[k] == b'\'') {
                let quote = bytes[k];
                out.push(quote as char);
                k += 1;
                let end = find_byte(bytes, k, quote);
                out.push_str(&normalize_attr_value(&source[k..end]));
                if end < len {
                    out.push(quote as char);
                    k = end + 1;
                } else {
                    k = end;
                }
            } else {
                let start = k;
                while k < len && !bytes[k].is_ascii_whitespace() && bytes[k] != b'>' {
                    k += 1;
                }
                let mut value = &source[start..k];
                let mut self_close = false;
                if value.ends_with('/') && bytes.get(k) == Some(&b'>') {
                    value = &value[..value.len() - 1];
                    self_close = true;
                }
                out.push_str(&normalize_attr_value(value));
                if self_close {
                    out.push('/');
                }
            }
        }
    }
    if name == "script" || name == "style" {
        return copy_raw_text(source, bytes, k, &name, out, false);
    }
    if name == "title" || name == "textarea" {
        return copy_raw_text(source, bytes, k, &name, out, true);
    }
    Ok(k)
}

/// Copy a raw-text body through its matching close tag. `title` and
/// `textarea` decode entities; `script` and `style` do not.
fn copy_raw_text(
    source: &str,
    bytes: &[u8],
    mut i: usize,
    name: &str,
    out: &mut String,
    decode_entities: bool,
) -> Result<usize, Error> {
    let len = bytes.len();
    let close = format!("</{name}");
    loop {
        if i >= len {
            return Ok(i);
        }
        if let Some(end) = close_tag_end(bytes, source, i, &close) {
            out.push_str(&source[i..end]);
            return Ok(end);
        }
        if decode_entities && bytes[i] == b'&' {
            let (replacement, next) = text_reference(source, i)?;
            out.push_str(&replacement);
            i = next;
        } else {
            // Tags never open inside raw text or RCDATA; every other
            // character is literal either way.
            let ch = source[i..].chars().next().unwrap_or('\u{FFFD}');
            out.push(ch);
            i += ch.len_utf8();
        }
    }
}

/// One `&`-sequence in text under `html.parser`'s rules: the replacement
/// (rewritten where the tokenizers disagree) and the next position.
///
/// The name runs over letters, digits, dots, and dashes; what follows it
/// decides. A semicolon decodes known names and drops off unknown ones; a
/// non-alphanumeric (or the end of input) decodes legacy names and leaves
/// the rest literal — except a decodable legacy prefix, which is blocked
/// with a numeric spelling of its join so the downstream tokenizer cannot
/// read it either.
fn text_reference(source: &str, i: usize) -> Result<(String, usize), Error> {
    let bytes = source.as_bytes();
    let len = bytes.len();
    if bytes.get(i + 1) == Some(&b'#') {
        return char_reference(source, i);
    }
    if bytes.get(i + 1).is_some_and(|b| b.is_ascii_alphabetic()) {
        let mut j = i + 2;
        while j < len && (bytes[j].is_ascii_alphanumeric() || bytes[j] == b'-' || bytes[j] == b'.')
        {
            j += 1;
        }
        let name = &source[i + 1..j];
        if bytes.get(j) == Some(&b';') {
            if html_entity(name).is_some() {
                // Agreed decoding; the spelling already matches.
                return Ok((source[i..j + 1].to_owned(), j + 1));
            }
            // Unknown references lose their semicolon upstream.
            let (head, skip) = drop_semicolon(name, &source[j + 1..]);
            return Ok((head, j + 1 + skip));
        }
        if html_entity(name).is_some() {
            // Agreed legacy decoding; the spelling already matches.
            return Ok((source[i..j].to_owned(), j));
        }
        if let Some(blocked) = block_name(name) {
            return Ok((blocked, j));
        }
        return Ok((source[i..j].to_owned(), j));
    }
    // `&` followed by anything else is literal on both sides.
    Ok(("&".to_owned(), i + 1))
}

/// `&UNKNOWN;` minus its semicolon, and glued: upstream consumes the
/// semicolon and scans on, so `&u;amp;` reads `&uamp;` — the rest is text,
/// never rescanned for entities. The rewrite mirrors that exactly: the head
/// plus the rest verbatim, except an alphanumeric join would glue into one
/// name downstream (`&uacute;` decodes where `&u` + `acute;` must not), so
/// the head is spelled numerically there (`&#38;uacute;`); a following
/// semicolon is spelled numerically too, consuming it.
fn drop_semicolon(name: &str, rest: &str) -> (String, usize) {
    let mut chars = rest.chars();
    let Some(c) = chars.next() else {
        return (block_name(name).unwrap_or_else(|| format!("&{name}")), 0);
    };
    if c.is_ascii_alphanumeric() {
        let head = format!("&#38;{name}");
        return (head, 0);
    }
    if c == ';' {
        let mut out = block_name(name).unwrap_or_else(|| format!("&{name}"));
        out.push_str("&#59;");
        return (out, 1);
    }
    (block_name(name).unwrap_or_else(|| format!("&{name}")), 0)
}

/// Spell a literal `&`-led name so no prefix of it can decode: `&lt3`
/// reads `<3` downstream but `&lt3` upstream, so the ampersand itself is
/// spelled numerically (`&#38;lt3`), which decodes to the same literal on
/// both sides. Names without a decodable prefix need nothing. Returns the
/// rewritten `&`-sequence.
pub(crate) fn block_name(name: &str) -> Option<String> {
    longest_legacy_prefix(name)?;
    Some(format!("&#38;{name}"))
}

/// The longest leading run of `name` that decodes as a legacy reference,
/// leaving a non-empty rest behind.
pub(crate) fn longest_legacy_prefix(name: &str) -> Option<String> {
    let mut best: Option<String> = None;
    for (index, _) in name.char_indices().skip(1) {
        let (prefix, rest) = name.split_at(index);
        if !rest.is_empty() && html_entity(prefix).is_some() {
            best = Some(prefix.to_owned());
        }
    }
    best
}

/// One `&#`-sequence in text: decoded where valid, an error where
/// `html.parser` raises, literal where it goes literal.
///
/// Valid digits plus a semicolon, a non-hex-digit, or the end of input
/// decode on both sides with the spelling unchanged. Digits chased by a
/// hex digit can never terminate, and at the end of input the whole rest
/// goes to `int()` — which raises. Anything else (`&#;`, `&#x`, a bare
/// `&#`) is literal `&#` with the scan resuming past it.
fn char_reference(source: &str, i: usize) -> Result<(String, usize), Error> {
    let bytes = source.as_bytes();
    let len = bytes.len();
    let mut j = i + 2;
    let hex = bytes.get(j) == Some(&b'x') || bytes.get(j) == Some(&b'X');
    if hex {
        j += 1;
    }
    let start = j;
    while j < len {
        let digit = (bytes[j] as char).is_ascii_digit();
        let hex_digit = (bytes[j] as char).is_ascii_hexdigit();
        if digit || (hex && hex_digit) {
            j += 1;
        } else {
            break;
        }
    }
    if j == start {
        // No digits at all: literal `&#`, rescanned past it.
        return Ok(("&#".to_owned(), i + 2));
    }
    match bytes.get(j) {
        Some(b';') => Ok((source[i..j + 1].to_owned(), j + 1)),
        None => Ok((source[i..j].to_owned(), j)),
        Some(b) if !((*b as char).is_ascii_hexdigit()) => Ok((source[i..j].to_owned(), j)),
        _ => Err(Error::Parse(format!(
            "malformed character reference at byte {i}"
        ))),
    }
}

/// An attribute value under `html.unescape`'s rules: exact hits decode,
/// misses stand, numerics always decode. Only legacy hits without their
/// semicolon need rewriting (attributes never decode those); misses
/// already agree, kept semicolons included.
fn normalize_attr_value(value: &str) -> String {
    let bytes = value.as_bytes();
    let len = bytes.len();
    let mut out = String::with_capacity(value.len());
    let mut i = 0;
    while i < len {
        if bytes[i] != b'&' {
            let ch = value[i..].chars().next().unwrap_or('\u{FFFD}');
            out.push(ch);
            i += ch.len_utf8();
            continue;
        }
        let mut j = i + 1;
        let numeric = bytes.get(j) == Some(&b'#');
        if numeric {
            j += 1;
            if bytes.get(j) == Some(&b'x') || bytes.get(j) == Some(&b'X') {
                j += 1;
                while j < len && (bytes[j] as char).is_ascii_hexdigit() {
                    j += 1;
                }
            } else {
                while j < len && bytes[j].is_ascii_digit() {
                    j += 1;
                }
            }
        } else {
            if !bytes.get(j).is_some_and(|b| b.is_ascii_alphabetic()) {
                out.push('&');
                i += 1;
                continue;
            }
            j += 1;
            while j < len && bytes[j].is_ascii_alphanumeric() {
                j += 1;
            }
        }
        let end = if bytes.get(j) == Some(&b';') || bytes.get(j) == Some(&b'=') {
            j + 1
        } else {
            j
        };
        let body = &value[i + 1..j];
        let key = format!("{body}{}", &value[j..end]);
        if numeric || html5_entity(&key).is_some() {
            if !numeric && end == j {
                // A legacy hit without its semicolon: decoded upstream,
                // kept literal downstream — spell it out.
                out.push_str(&format!("&{body};"));
            } else {
                out.push_str(&value[i..end]);
            }
            i = end;
        } else {
            // Misses stand on both sides, semicolon included.
            out.push_str(&value[i..end]);
            i = end;
        }
    }
    out
}

/// The eight references HTML decodes without a semicolon — and libxml2's
/// HTML parser never does.
const NAV_LEGACY: [&str; 8] = ["amp", "AMP", "lt", "LT", "gt", "GT", "quot", "QUOT"];

/// Rewrite nav source so html5ever reads its text the way libxml2's HTML
/// parser does: named references decode only with a semicolon here, and
/// `&#;` vanishes. Everything else already agrees. Infallible — libxml2's
/// HTML parser does not raise on entities.
pub fn normalize_nav_entities(source: &str) -> String {
    let bytes = source.as_bytes();
    let len = bytes.len();
    let mut out = String::with_capacity(source.len());
    let mut i = 0;
    while i < len {
        if bytes[i] == b'&' {
            let (replacement, next) = nav_reference(source, i);
            out.push_str(&replacement);
            i = next;
        } else if bytes[i] == b'<' {
            i = copy_nav_markup(source, bytes, i, &mut out);
        } else {
            let ch = source[i..].chars().next().unwrap_or('\u{FFFD}');
            out.push(ch);
            i += ch.len_utf8();
        }
    }
    out
}

/// Copy nav markup verbatim, skipping comments, declarations, processing
/// instructions, and `script`/`style` bodies (raw on both sides). Attribute
/// values are never rewritten: only presence and emptiness are read.
fn copy_nav_markup(source: &str, bytes: &[u8], i: usize, out: &mut String) -> usize {
    let len = bytes.len();
    if starts_with_ignore_case(bytes, i, "<!--") {
        let end = find_bytes(bytes, i + 4, b"-->");
        out.push_str(&source[i..end]);
        return end;
    }
    if starts_with_ignore_case(bytes, i, "<!") {
        // Bogus comments and doctypes end at the first `>` downstream too.
        let end = find_byte(bytes, i + 2, b'>');
        let end = end + usize::from(end < len);
        out.push_str(&source[i..end]);
        return end;
    }
    if starts_with_ignore_case(bytes, i, "<?") {
        let end = skip_decl(bytes, i + 2);
        out.push_str(&source[i..end]);
        return end;
    }
    if starts_with_ignore_case(bytes, i, "</") {
        let end = find_byte(bytes, i + 2, b'>');
        let end = end + usize::from(end < len);
        out.push_str(&source[i..end]);
        return end;
    }
    if i + 1 < len && bytes[i + 1].is_ascii_alphabetic() {
        let mut j = i + 1;
        while j < len && bytes[j].is_ascii_alphanumeric() {
            j += 1;
        }
        let name = source[i + 1..j].to_lowercase();
        let end = skip_tag(bytes, j);
        out.push_str(&source[i..end]);
        if name == "script" || name == "style" {
            return copy_nav_raw(source, bytes, end, &name, out);
        }
        return end;
    }
    out.push('<');
    i + 1
}

/// Skip an open tag's remainder, quoted `>` characters included. An
/// unterminated quote runs to the end of input, never past it.
fn skip_tag(bytes: &[u8], mut k: usize) -> usize {
    while k < bytes.len() {
        match bytes[k] {
            b'"' | b'\'' => {
                k = find_byte(bytes, k + 1, bytes[k]);
                k += usize::from(k < bytes.len());
            }
            b'>' => return k + 1,
            _ => k += 1,
        }
    }
    k
}

/// Copy a `script`/`style` body through its close tag, unprocessed.
fn copy_nav_raw(source: &str, bytes: &[u8], mut i: usize, name: &str, out: &mut String) -> usize {
    let close = format!("</{name}");
    loop {
        if i >= bytes.len() {
            return i;
        }
        if let Some(end) = close_tag_end(bytes, source, i, &close) {
            out.push_str(&source[i..end]);
            return end;
        }
        let ch = source[i..].chars().next().unwrap_or('\u{FFFD}');
        out.push(ch);
        i += ch.len_utf8();
    }
}

/// One `&`-sequence in nav text: unknown and numeric spellings already
/// agree — only the semicolon-less legacy hits and `&#;` need rewriting.
fn nav_reference(source: &str, i: usize) -> (String, usize) {
    let bytes = source.as_bytes();
    let len = bytes.len();
    if bytes.get(i + 1) == Some(&b'#') {
        if source[i + 2..].starts_with(';') {
            // `&#;` vanishes upstream; kept literal downstream.
            return (String::new(), i + 3);
        }
        return ("&".to_owned(), i + 1);
    }
    if bytes.get(i + 1).is_some_and(|b| b.is_ascii_alphabetic()) {
        let mut j = i + 2;
        while j < len && (bytes[j].is_ascii_alphanumeric() || bytes[j] == b'-' || bytes[j] == b'.')
        {
            j += 1;
        }
        let name = &source[i + 1..j];
        if bytes.get(j) == Some(&b';') {
            return (source[i..j + 1].to_owned(), j + 1);
        }
        if NAV_LEGACY.contains(&name) || block_name(name).is_some() {
            return (format!("&#38;{name}"), j);
        }
        return (source[i..j].to_owned(), j);
    }
    ("&".to_owned(), i + 1)
}
