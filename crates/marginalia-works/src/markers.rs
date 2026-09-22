//! `{{cite:<key>}}` markers — the one bijection validation judges.
//!
//! Python source: `services/works/markers.py`. Every reader of markers uses
//! [`MARKER_RE`], so a marker no reader sees cannot exist.

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use uuid::Uuid;

/// A marker names its occurrence's `citation_key`, a UUID.
pub static MARKER_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\{\{cite:([^}]+)\}\}").expect("marker regex"));

/// The marker text for an occurrence, placed by the caller.
#[must_use]
pub fn format_marker(citation_key: &Uuid) -> String {
    format!("{{{{cite:{citation_key}}}}}")
}

/// Split a block's markers into valid citation-key strings and dangling raw
/// text. A `{{cite:…}}` whose key is not a UUID can match no occurrence, so
/// it is reported dangling without a lookup. Returns `(keys, invalid)`.
///
/// Key normalization mirrors `str(UUID(raw))`: `Uuid::parse_str` alone is
/// stricter than CPython (it rejects a stray brace left by the `[^}]+`
/// match and demands hyphen positions), so [`parse_marker_key`] replicates
/// the `UUID.__init__` hex path instead.
pub fn find_markers(text: &str) -> (HashSet<String>, Vec<String>) {
    let mut keys = HashSet::new();
    let mut invalid = Vec::new();
    for capture in MARKER_RE.captures_iter(text) {
        let raw = &capture[1];
        match parse_marker_key(raw) {
            Some(key) => {
                keys.insert(key);
            }
            None => invalid.push(capture[0].to_owned()),
        }
    }
    (keys, invalid)
}

/// Normalize a marker key exactly as `str(UUID(raw))` does: remove every
/// `urn:` then every `uuid:` (case-sensitive), strip surrounding braces,
/// ignore all hyphens, then parse the 32-char remainder as CPython's
/// `int(hex, 16)` does and re-emit lowercase hyphenated hex.
///
/// The `int` extras that survive the length check are reproduced for ASCII:
/// surrounding whitespace, one leading `+` (a `-` fails the range check),
/// a `0x` prefix, and single underscores between digits, with the value
/// zero-padded back to 32 hex digits. Non-ASCII decimal digits (accepted by
/// CPython, e.g. Arabic-Indic) are NOT reproduced and stay dangling: Rust's
/// digit tables are ASCII-only, so no faithful mapping exists here.
fn parse_marker_key(raw: &str) -> Option<String> {
    let without_prefix = raw.replace("urn:", "").replace("uuid:", "");
    let braced = without_prefix.trim_matches(|cell| cell == '{' || cell == '}');
    let compact: String = braced.chars().filter(|cell| *cell != '-').collect();
    if compact.chars().count() != 32 {
        return None;
    }
    let trimmed = compact.trim_matches(|cell: char| cell.is_whitespace());
    let signed = trimmed.strip_prefix('+').unwrap_or(trimmed);
    // No `-` guard here: `compact` filtered out every `-`, and trimming
    // whitespace / stripping one `+` never introduces one, so `signed`
    // cannot start with `-`. A leading dash fails the 32-char count above
    // instead (it is filtered with the hyphens), matching CPython's range
    // rejection of the negative value.
    let prefixed = signed
        .strip_prefix("0x")
        .or_else(|| signed.strip_prefix("0X"))
        .unwrap_or(signed);
    let mut digits = String::with_capacity(32);
    let mut previous_underscore = false;
    for (index, cell) in prefixed.char_indices() {
        if cell == '_' {
            // CPython allows one underscore strictly between two digits.
            let next_is_digit = prefixed[index + 1..]
                .chars()
                .next()
                .is_some_and(|next| next.is_ascii_hexdigit());
            if previous_underscore || digits.is_empty() || !next_is_digit {
                return None;
            }
            previous_underscore = true;
            continue;
        }
        if !cell.is_ascii_hexdigit() {
            return None;
        }
        previous_underscore = false;
        digits.push(cell);
    }
    if digits.is_empty() {
        return None;
    }
    // Fewer than 32 digits (underscores stood in for some) parse to a
    // smaller value that `str(UUID)` zero-pads; `u128` holds the full 128
    // bits either way. Proof the parse cannot fail: `compact` is exactly 32
    // chars (checked above) and the loop pushes only ascii-hexdigit cells,
    // returning `None` on anything else, so `digits` holds 1..=32 hexdigits.
    let value = u128::from_str_radix(&digits, 16).expect("marker digits parse");
    let hex = format!("{value:032x}");
    Some(format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY_A: &str = "12345678-1234-5678-1234-567812345678";
    const KEY_B: &str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";

    fn sorted_keys(keys: &HashSet<String>) -> Vec<&str> {
        let mut out: Vec<&str> = keys.iter().map(String::as_str).collect();
        out.sort_unstable();
        out
    }

    #[test]
    fn test_format_and_find_round_trip() {
        // Mirrors `TestMarkers::test_format_and_find_round_trip`.
        let key = Uuid::parse_str(KEY_A).expect("valid test UUID");
        assert_eq!(format_marker(&key), format!("{{{{cite:{KEY_A}}}}}"));
        let (keys, invalid) = find_markers(&format!("a {marker} b", marker = format_marker(&key)));
        assert_eq!(keys, HashSet::from([KEY_A.to_owned()]));
        assert!(invalid.is_empty());
    }

    #[test]
    fn test_non_uuid_marker_is_invalid_not_silent() {
        // Mirrors `TestMarkers::test_non_uuid_marker_is_invalid_not_silent`.
        let (keys, invalid) = find_markers("see {{cite:c1}} here");
        assert!(keys.is_empty());
        assert_eq!(invalid, vec!["{{cite:c1}}".to_owned()]);
    }

    #[test]
    fn duplicate_markers_dedupe_to_one_key() {
        let text = format!("a {{{{cite:{KEY_A}}}}} b {{{{cite:{KEY_A}}}}} c {{{{cite:{KEY_B}}}}}");
        let (keys, invalid) = find_markers(&text);
        assert_eq!(sorted_keys(&keys), vec![KEY_A, KEY_B]);
        assert!(invalid.is_empty());
    }

    #[test]
    fn uppercase_uuid_normalizes_lowercase() {
        // `str(UUID(raw))` lowercases; `Uuid::to_string` must match.
        let (keys, invalid) = find_markers(&format!("{{{{cite:{}}}}}", KEY_A.to_uppercase()));
        assert_eq!(keys, HashSet::from([KEY_A.to_owned()]));
        assert!(invalid.is_empty());
    }

    #[test]
    fn braced_urn_and_simple_spellings_parse() {
        // `UUID(raw)` accepts braced, urn, and 32-hex spellings, normalizing
        // to hyphenated lowercase — including a lone leading brace left by
        // the `[^}]+` match, which `strip('{}')` removes.
        let simple = KEY_A.replace('-', "");
        for raw in [format!("{{{KEY_A}}}"), format!("urn:uuid:{KEY_A}"), simple] {
            let (keys, invalid) = find_markers(&format!("{{{{cite:{raw}}}}}"));
            assert_eq!(keys, HashSet::from([KEY_A.to_owned()]), "raw {raw}");
            assert!(invalid.is_empty());
        }
    }

    #[test]
    fn misplaced_hyphens_still_parse() {
        // CPython ignores every hyphen before the length check, so
        // hyphen-position typos still name the key.
        let (keys, invalid) = find_markers("{{cite:12345678-123456781234567812345678}}");
        assert_eq!(keys, HashSet::from([KEY_A.to_owned()]));
        assert!(invalid.is_empty());
    }

    #[test]
    fn int_spellings_match_python() {
        // Survivors of CPython's `int(hex, 16)`: inner underscores,
        // surrounding whitespace, a leading `+`, and a `0x` prefix all
        // parse (zero-padded back to 32 digits); a leading `-` fails the
        // range check and an uppercase `URN:` prefix is never stripped.
        let valid: &[(&str, &str)] = &[
            (
                "1234567_123456781234567812345678",
                "01234567-1234-5678-1234-567812345678",
            ),
            (
                "  123456781234567812345678123456",
                "00123456-7812-3456-7812-345678123456",
            ),
            (
                "+1234567812345678123456781234567",
                "01234567-8123-4567-8123-456781234567",
            ),
            (
                "0x123456781234567812345678123456",
                "00123456-7812-3456-7812-345678123456",
            ),
        ];
        for (raw, expected) in valid {
            let (keys, invalid) = find_markers(&format!("{{{{cite:{raw}}}}}"));
            assert_eq!(keys, HashSet::from([(*expected).to_owned()]), "raw {raw}");
            assert!(invalid.is_empty());
        }
        let dangling = [
            "-1234567812345678123456781234567",
            "URN:UUID:12345678-1234-5678-1234-567812345678",
            "_1234567812345678123456781234567",
            "1234567__12345678123456781234567",
        ];
        for raw in dangling {
            let (keys, invalid) = find_markers(&format!("{{{{cite:{raw}}}}}"));
            assert!(keys.is_empty(), "raw {raw}");
            assert_eq!(invalid, vec![format!("{{{{cite:{raw}}}}}")]);
        }
    }

    #[test]
    fn empty_text_and_empty_key_find_nothing() {
        // `{{cite:}}` cannot match: `[^}]+` needs a key, and `UUID("")`
        // raises anyway.
        for text in ["", "{{cite:}}", "no markers here", "{{cite:}}"] {
            let (keys, invalid) = find_markers(text);
            assert!(keys.is_empty(), "text {text:?}");
            assert!(invalid.is_empty(), "text {text:?}");
        }
    }

    #[test]
    fn nested_braces_report_truncated_dangling() {
        // `[^}]+` stops at the first `}`, so the inner marker is swallowed
        // into one dangling span that no UUID parse accepts.
        let text = format!("{{{{cite:{{{{cite:{KEY_A}}}}}}}}}");
        let (keys, invalid) = find_markers(&text);
        assert!(keys.is_empty());
        assert_eq!(invalid, vec![format!("{{{{cite:{{{{cite:{KEY_A}}}}}")]);
    }

    #[test]
    fn unclosed_marker_finds_nothing() {
        let (keys, invalid) = find_markers(&format!("{{{{cite:{KEY_A}}}"));
        assert!(keys.is_empty());
        assert!(invalid.is_empty());
    }

    #[test]
    fn padded_uuid_is_dangling() {
        // `UUID(" <key> ")` rejects surrounding whitespace, so a padded
        // marker is dangling rather than a match.
        let (keys, invalid) = find_markers(&format!("{{{{cite: {KEY_A} }}}}"));
        assert!(keys.is_empty());
        assert_eq!(invalid, vec![format!("{{{{cite: {KEY_A} }}}}")]);
    }

    #[test]
    fn non_hex_and_blank_keys_are_dangling() {
        // 32 hex-position characters with no hex value (`UUID()` raises
        // `ValueError` on the `int(hex, 16)` parse), and 32 blanks (the
        // remainder trims to nothing, so no digits survive to parse):
        // both dangle with the raw marker text preserved.
        for raw in ["z".repeat(32), " ".repeat(32)] {
            let (keys, invalid) = find_markers(&format!("{{{{cite:{raw}}}}}"));
            assert!(keys.is_empty(), "raw {raw:?}");
            assert_eq!(invalid, vec![format!("{{{{cite:{raw}}}}}")]);
        }
    }
}
