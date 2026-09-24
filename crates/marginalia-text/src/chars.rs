//! Python `str` predicates the text transforms depend on.
//!
//! `char::is_whitespace` is the Unicode White_Space property, which misses
//! U+001C–U+001F that `str.isspace()` includes. That delta was verified by
//! sweeping CPython's `str.isspace()` over all 1,114,112 code points against
//! this predicate (see the differential): the four file/group separators are
//! the entire difference.
//!
//! The same sweep shows Python `re` `\s` (str) matches exactly the
//! `str.isspace()` set — 10 ranges, all BMP — so this predicate is also the
//! definition the regex character classes spell out.

/// One character's `str.isspace()` verdict.
#[inline]
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()` — both ends, Python's character set.
#[inline]
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}
