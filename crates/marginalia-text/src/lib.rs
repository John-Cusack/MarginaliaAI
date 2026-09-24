//! Text core behind the shipped seams: quote normalization, token
//! estimates, span-preserving splits, and markdown section recovery.
//!
//! Python sources: `services/text/{normalize,tokens,sections}.py` and
//! `sdk/chunking.py`. All offsets are character offsets, exactly as Python
//! string indices are.

pub mod chars;
pub mod normalize;
pub mod repr;
pub(crate) mod repr_table;
pub mod sections;
pub mod spans;
pub mod tokens;
pub mod word_table;

/// The Unicode version every character table in this crate implements:
/// NFKC (`unicode-normalization`, pinned), the baked `\w` table, and the
/// `str.isspace()` set. It is CPython 3.13's `unicodedata.unidata_version`;
/// a Python with different tables gives different answers for some code
/// points, so the Python side only routes `auto` here when the versions match.
pub const UNICODE_VERSION: &str = "15.1.0";

#[cfg(test)]
mod tests {
    #[test]
    fn unicode_version_is_the_nfkc_tables_version() {
        let (major, minor, micro) = unicode_normalization::UNICODE_VERSION;
        assert_eq!(super::UNICODE_VERSION, format!("{major}.{minor}.{micro}"));
    }
}
