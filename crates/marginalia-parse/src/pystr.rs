//! Python `str` operations the parsers and bins depend on, spelled once.
//!
//! `marginalia_text::chars` already owns `is_space` (the `str.isspace()` set,
//! swept over all 1,114,112 code points in Phase 1) and whole-string `strip`.
//! This module adds the remaining pieces parser code actually calls:
//! `splitlines`, one-sided stripping, `pathlib` stem splitting, and the
//! single-character `isupper`/`islower` verdicts `bible_layout` reads off
//! block initials.
//!
//! `is_upper`/`is_lower` are baked verdict tables (`case_tables`), not
//! category tests: single-character verdicts follow CPython, where even
//! non-`Lu` letters like U+2160 ROMAN NUMERAL ONE count as upper.

use marginalia_text::chars::is_space;

use crate::case_tables::{is_lower as table_lower, is_upper as table_upper};

/// `str.splitlines()`: boundaries are `\n`, `\r`, `\r\n`, `\x0b`, `\x0c`,
/// `\x1c`–`\x1e`, `\x85`, `\u{2028}`, and `\u{2029}`. A trailing break adds
/// no empty line. Returns byte ranges so callers can slice without copying.
pub fn splitlines_ranges(text: &str) -> Vec<(usize, usize)> {
    let bytes = text.as_bytes();
    let mut out = Vec::new();
    let mut start = 0usize;
    let mut i = 0usize;
    // Byte lengths of the multi-byte boundaries, keyed by first byte.
    while i < bytes.len() {
        let b = bytes[i];
        let single = matches!(b, b'\n' | b'\r' | 0x0b | 0x0c | 0x1c | 0x1d | 0x1e);
        let mut next = i + 1;
        if single {
            // `\r\n` is one boundary, not two.
            if b == b'\r' && bytes.get(i + 1) == Some(&b'\n') {
                next = i + 2;
            }
        } else if b == 0xC2 && bytes.get(i + 1) == Some(&0x85) {
            next = i + 2; // U+0085 NEXT LINE
        } else if b == 0xE2
            && bytes.get(i + 1) == Some(&0x80)
            && (bytes.get(i + 2) == Some(&0xA8) || bytes.get(i + 2) == Some(&0xA9))
        {
            next = i + 3; // U+2028 / U+2029
        } else {
            i += 1;
            continue;
        }
        out.push((start, i));
        start = next;
        i = next;
    }
    // A break at the very end contributes no trailing empty piece; any other
    // remainder does. The empty string splits to no lines at all.
    if start < bytes.len() {
        out.push((start, bytes.len()));
    }
    out
}

/// `len(text.splitlines())` without building the lines.
pub fn line_count(text: &str) -> usize {
    splitlines_ranges(text).len()
}

/// `str.splitlines()` as owned lines.
pub fn splitlines(text: &str) -> Vec<&str> {
    splitlines_ranges(text)
        .iter()
        .map(|&(s, e)| &text[s..e])
        .collect()
}

/// `str.lstrip()` — Python's character set, not Rust's.
pub fn lstrip(s: &str) -> &str {
    s.trim_start_matches(is_space)
}

/// `str.rstrip()` — Python's character set, not Rust's.
pub fn rstrip(s: &str) -> &str {
    s.trim_end_matches(is_space)
}

/// `pathlib.Path.name`'s stem: text before the last dot, except a leading
/// dot never starts a suffix (`Path(".bashrc").stem == ".bashrc"`).
pub fn py_stem(file_name: &str) -> &str {
    let base = file_name.rsplit(['/', '\\']).next().unwrap_or(file_name);
    match base.rfind('.') {
        Some(0) | None => base,
        Some(i) => &base[..i],
    }
}

/// Single-character `str.isupper()`, baked from the interpreter
/// (651 ranges under CPython 3.13, Unicode 15.1).
pub fn is_upper(c: char) -> bool {
    table_upper(c)
}

/// Single-character `str.islower()`, baked from the interpreter
/// (671 ranges under CPython 3.13, Unicode 15.1).
pub fn is_lower(c: char) -> bool {
    table_lower(c)
}
