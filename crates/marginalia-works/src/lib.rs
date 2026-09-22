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
pub mod publication;
pub mod render;
pub mod trace;
pub mod validate;
pub mod verify;

use std::cmp::Ordering;

/// Python `repr` of a list of `str`: `[` + comma-space-joined reprs + `]`,
/// exactly what `f"{unknown}"` renders for the unknown-keys error.
/// Item rendering lives in [`marginalia_text::repr`] (moved there so every
/// crate that formats a Python-visible message shares it).
pub(crate) fn py_repr_str_list(items: &[String]) -> String {
    let mut out = String::from("[");
    for (index, item) in items.iter().enumerate() {
        if index > 0 {
            out.push_str(", ");
        }
        out.push_str(&marginalia_text::repr::py_repr_str(item));
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
    fn repr_list_joins_item_reprs() {
        // Item rendering moved to `marginalia_text::repr` (pinned there);
        // this pins the list shape that stays here.
        assert_eq!(py_repr_str_list(&[]), "[]");
        assert_eq!(py_repr_str_list(&["a".to_owned()]), "['a']");
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
