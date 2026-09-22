//! Phase 4 works-authorship pure core: hashing, markers, work files,
//! revision assembly, drafting round-trip, render, verification and
//! validation rules, grounding trace, citation attach, publication gates,
//! and epistolary date parsing.
//!
//! Python sources: `services/works/{hashing,markers,citations,assembly,
//! render,files,drafting,verify,validate,trace,attach,publication}.py` and
//! `services/text/dates.py`.
//!
//! Every `async` service is generic over the `marginalia_types` port traits
//! it reads or writes; DB-touching behavior stays Python until Phase 5 and
//! the traits are unimplemented in Rust. What ports 1:1 here is the
//! deterministic core: canonical JSON and sha256, marker bijection,
//! front-matter parsing, markdown export/import, footnote text, gate and
//! severity rules, trace labels, refusal decisions, and date arithmetic.
//!
//! Explicitly NOT here (stay Python until Phase 5): `cite.py` (DB writer
//! beyond the pure refusal rules, which live in [`attach`]) and
//! `work_service.py` (rows/transactions).

pub mod assembly;
pub mod attach;
pub mod citations;
pub mod dates;
pub mod drafting;
pub mod files;
pub mod hashing;
pub mod markers;
pub(crate) mod printable_table;
pub mod publication;
pub mod render;
pub mod trace;
pub mod validate;
pub mod verify;

use std::cmp::Ordering;

/// Python `repr` of a `str`: single quotes unless the value holds one (then
/// double), with `\\`, `\n`, `\r`, `\t`, and non-printables escaped the way
/// CPython's `unicode_repr` does. Only error-message text rides on this —
/// rule ids are the contract — but the messages stay byte-identical anyway.
pub(crate) fn py_repr_str(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(value.len() + 2);
    out.push(quote);
    for ch in value.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            // `quote == '"'` holds only when the value holds `'` and no `"`,
            // so a `"` char can never meet a `"` quote here: no arm for it.
            // (Python never escapes `"` inside a double-quoted repr either —
            // the selection guarantees it is absent.)
            '\'' if quote == '\'' => out.push_str("\\'"),
            ch if is_py_printable(ch) => out.push(ch),
            ch => {
                let code = ch as u32;
                if code < 0x100 {
                    out.push_str(&format!("\\x{code:02x}"));
                } else if code < 0x10000 {
                    out.push_str(&format!("\\u{code:04x}"));
                } else {
                    out.push_str(&format!("\\U{code:08x}"));
                }
            }
        }
    }
    out.push(quote);
    out
}

/// `str.isprintable()` as data: CPython escapes a char in `repr` iff its
/// general category is Cc/Cf/Cs/Co/Cn/Zl/Zp/Zs — except U+0020 SPACE, which
/// passes through raw. The categories are Unicode-version data, so they live
/// in [`printable_table::UNPRINTABLE_RANGES`] (generated from the running
/// Python by `etc/gen_printable_table.py`), not in hand-written ranges:
/// hand lists under-escape Zs/Cn/Co and over-escape Lo/Mn (probed: 1,089
/// divergences over U+0000–U+2FFF before the table).
fn is_py_printable(ch: char) -> bool {
    if ch == ' ' {
        return true;
    }
    let code = ch as u32;
    !printable_table::UNPRINTABLE_RANGES
        .iter()
        .any(|(lo, hi)| *lo <= code && code <= *hi)
}

/// Python `repr` of a list of `str`: `[` + comma-space-joined [`py_repr_str`]
/// + `]`, exactly what `f"{unknown}"` renders for the unknown-keys error.
pub(crate) fn py_repr_str_list(items: &[String]) -> String {
    let mut out = String::from("[");
    for (index, item) in items.iter().enumerate() {
        if index > 0 {
            out.push_str(", ");
        }
        out.push_str(&py_repr_str(item));
    }
    out.push(']');
    out
}

/// Compare digit strings by numeric value: leading zeros do not order.
/// Mirrors `int(a) < int(b)` for the footnote `cN` sort without big-int
/// parsing (citation handles are short, but unbounded in principle).
pub(crate) fn cmp_numeric_strings(a: &str, b: &str) -> Ordering {
    let digits_a = a.trim_start_matches('0');
    let digits_b = b.trim_start_matches('0');
    digits_a
        .len()
        .cmp(&digits_b.len())
        .then_with(|| digits_a.cmp(digits_b))
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn printable_table_size_is_pinned() {
        // Regenerate only on Python/Unicode-data upgrades; any drift here
        // means `repr` escaping moved under us.
        assert_eq!(printable_table::UNPRINTABLE_RANGES.len(), 713);
    }

    #[test]
    fn repr_escapes_match_cpython_boundaries() {
        assert_eq!(py_repr_str("a'b"), "\"a'b\"");
        assert_eq!(py_repr_str("a'b\"c"), "'a\\'b\"c'");
        assert_eq!(py_repr_str("a\\b\nc\td\re"), "'a\\\\b\\nc\\td\\re'");
        assert_eq!(py_repr_str("\u{a0}"), "'\\xa0'");
        assert_eq!(py_repr_str("\u{3000}"), "'\\u3000'");
        assert_eq!(py_repr_str("\u{115f}"), "'\u{115f}'");
        assert_eq!(py_repr_str("é"), "'é'");
        assert_eq!(py_repr_str("😀"), "'😀'");
        assert_eq!(py_repr_str("\0"), "'\\x00'");
        assert_eq!(py_repr_str("\x7f"), "'\\x7f'");
        assert_eq!(py_repr_str("\u{ad}"), "'\\xad'");
        assert_eq!(py_repr_str("\u{e0001}"), "'\\U000e0001'");
        assert_eq!(
            py_repr_str_list(&["a".to_owned(), "b'c".to_owned()]),
            "['a', \"b'c\"]"
        );
    }

    #[test]
    fn numeric_compare_ignores_leading_zeros() {
        assert_eq!(cmp_numeric_strings("01", "1"), Ordering::Equal);
        assert_eq!(cmp_numeric_strings("2", "10"), Ordering::Less);
        assert_eq!(cmp_numeric_strings("c10", "c2"), Ordering::Greater);
    }
}
