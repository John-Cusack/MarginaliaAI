//! Python `repr` emulation for `str`, mirroring CPython's `unicode_repr`.
//!
//! Only error-message text rides on this — rule ids are the contract — but
//! the messages stay byte-identical anyway. The rule is data, not logic:
//! printability is [`repr_table::UNPRINTABLE_RANGES`] (generated from the
//! running Python by `etc/gen_printable_table.py`), never hand-written
//! ranges. Moved here from `marginalia-works` so every crate that formats a
//! Python-visible message shares the one implementation.

use crate::repr_table;

/// Python `repr` of a `str`: single quotes unless the value holds one (then
/// double), with `\\`, `\n`, `\r`, `\t`, and non-printables escaped the way
/// CPython's `unicode_repr` does.
pub fn py_repr_str(value: &str) -> String {
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
/// passes through raw.
pub fn is_py_printable(ch: char) -> bool {
    if ch == ' ' {
        return true;
    }
    let code = ch as u32;
    !repr_table::UNPRINTABLE_RANGES
        .iter()
        .any(|(lo, hi)| *lo <= code && code <= *hi)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn printable_table_size_is_pinned() {
        // Regenerate only on Python/Unicode-data upgrades; any drift here
        // means `repr` escaping moved under us.
        assert_eq!(repr_table::UNPRINTABLE_RANGES.len(), 713);
    }

    #[test]
    fn repr_matches_python_on_selection_and_escapes() {
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
        assert_eq!(py_repr_str(""), "''");
        assert_eq!(py_repr_str("'"), "\"'\"");
        assert_eq!(py_repr_str("\""), "'\"'");
        assert_eq!(py_repr_str("Missing entirely"), "'Missing entirely'");
        assert!(is_py_printable(' '));
        assert!(is_py_printable('a'));
        assert!(!is_py_printable('\n'));
    }
}
