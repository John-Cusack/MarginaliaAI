//! Text acceptance: parity with `test_normalize_map.py`, `test_chunking.py`,
//! `test_token_estimation.py`, and the markdown section suite — the parts
//! the shipped seams (normalization, both chunkers, markdown) stand on.

use marginalia_text::normalize::{
    normalize, normalize_for_matching, normalize_with_map, NORMALIZATION_VERSION,
};
use marginalia_text::sections::sections_from_markdown;
use marginalia_text::spans::{cap_spans, split_at_boundary, split_span, trim_span};
use marginalia_text::tokens::{
    approx_tokens, chars_per_token, min_chars_per_token, token_budget_chars, ABSOLUTE_MAX_TOKENS,
    DEFAULT_CHARS_PER_TOKEN,
};

fn chars(s: &str) -> Vec<char> {
    s.chars().collect()
}

// --- normalize ---

#[test]
fn version_pins_the_fold() {
    assert_eq!(NORMALIZATION_VERSION, "1.0");
}

#[test]
fn typography_folds() {
    assert_eq!(normalize("“justice”—not mercy"), "\"justice\"-not mercy");
    assert_eq!(normalize("‘single’ ‚and‛ ‹them›"), "'single' 'and' 'them'");
    assert_eq!(normalize("a–b—c―d−e‐f‑g"), "a-b-c-d-e-f-g");
    assert_eq!(normalize("the ﬁrst ruler"), "the first ruler");
    assert_eq!(normalize("  spaced\t\tout\n\n"), "spaced out");
}

#[test]
fn soft_hyphen_vanishes() {
    assert_eq!(normalize_for_matching("judg\u{AD}ment"), "judgment");
}

#[test]
fn linebreak_hyphen_joins_only_before_lowercase() {
    assert_eq!(normalize("fis-\ncal"), "fiscal");
    assert_eq!(normalize("Anglo-\nSaxon"), "Anglo- Saxon");
}

// --- normalize_with_map: the map is the whole point ---

fn roundtrip(raw: &str, needle: &str) -> (usize, usize) {
    let (normalized, index_map) = normalize_with_map(raw);
    let target = normalize_for_matching(needle);
    let at = normalized
        .find(&target)
        .unwrap_or_else(|| panic!("{target:?} not in {normalized:?}"));
    let at_chars = normalized[..at].chars().count();
    (
        index_map[at_chars],
        index_map[at_chars + target.chars().count() - 1] + 1,
    )
}

fn raw_slice(raw: &str, span: (usize, usize)) -> String {
    chars(raw)[span.0..span.1].iter().collect()
}

#[test]
fn folded_matches_report_raw_addresses() {
    let raw = "He said “justice and righteousness” loudly.";
    assert_eq!(
        raw_slice(raw, roundtrip(raw, "\"justice and righteousness\"")),
        "“justice and righteousness”"
    );

    let raw = "mishpat   and\n\n  tsedaqah";
    assert_eq!(raw_slice(raw, roundtrip(raw, "mishpat and tsedaqah")), raw);

    let raw = "a righ-\nteous ruler";
    assert_eq!(
        raw_slice(raw, roundtrip(raw, "righteous ruler")),
        "righ-\nteous ruler"
    );

    let raw = "judg\u{AD}ment";
    assert_eq!(raw_slice(raw, roundtrip(raw, "judgment")), raw);

    let raw = "justice—not mercy";
    assert_eq!(raw_slice(raw, roundtrip(raw, "justice-not mercy")), raw);
}

#[test]
fn ligature_expansion_shares_one_offset() {
    let raw = "the ﬁrst ruler";
    let (normalized, index_map) = normalize_with_map(raw);
    assert!(normalized.contains("first"));
    let at = normalized.find("first").unwrap();
    let at = normalized[..at].chars().count();
    let lig = raw.find('ﬁ').unwrap();
    let lig = raw[..lig].chars().count();
    assert_eq!((index_map[at], index_map[at + 1]), (lig, lig));
}

#[test]
fn folding_is_symmetric_and_the_map_is_sound() {
    for text in ["מִשְׁפָּט", "κρίσις", "plain ascii"] {
        let (normalized, index_map) = normalize_with_map(text);
        assert_eq!(normalize_for_matching(text), normalized);
        assert_eq!(index_map.len(), normalized.chars().count());
    }
    let raw = "He said  “justice”—a righ-\nteous word.";
    let (normalized, index_map) = normalize_with_map(raw);
    assert_eq!(index_map.len(), normalized.chars().count());
    assert!(index_map.windows(2).all(|w| w[0] <= w[1]));
    assert!(index_map.iter().all(|&i| i < raw.chars().count()));
}

// --- tokens ---

#[test]
fn token_rates_match_the_measured_table() {
    assert_eq!(DEFAULT_CHARS_PER_TOKEN, 4.0);
    assert_eq!(ABSOLUTE_MAX_TOKENS, 2_000);
    assert_eq!(chars_per_token("plain ascii"), 4.0);
    assert_eq!(chars_per_token(""), 4.0);
    // Denser scripts estimate more tokens per character.
    let hebrew = chars_per_token("מִשְׁפָּט צְדָקָה");
    let greek = chars_per_token("κρίσις δικαιοσύνη");
    assert!(hebrew < 4.0 && greek < 4.0, "{hebrew} {greek}");
    assert!(min_chars_per_token("מִשְׁפָּט abc") <= chars_per_token("מִשְׁפָּט abc"));
    assert_eq!(approx_tokens("", None), 1);
    assert_eq!(approx_tokens("abcd", Some(4.0)), 1);
    assert_eq!(approx_tokens("abcdefgh", Some(4.0)), 2);
    assert_eq!(token_budget_chars(100, 4.0), 400);
    assert_eq!(token_budget_chars(0, 4.0), 1);
}

// --- spans ---

#[test]
fn trim_moves_the_span_never_the_text() {
    let text = chars("  padded  ");
    assert_eq!(trim_span(&text, 0, 10), (2, 8));
    assert_eq!(trim_span(&text, 2, 8), (2, 8));
}

#[test]
fn split_rejects_bad_input_and_prefers_newlines() {
    let text = chars("aaa bbb\nccc ddd eee");
    assert!(split_at_boundary(&text, 0, text.len(), 0).is_err());
    assert!(split_at_boundary(&text, 5, 2, 4).is_err());
    let pieces = split_at_boundary(&text, 0, text.len(), 8).unwrap();
    // First piece ends at the newline, not mid-word.
    assert_eq!(pieces[0], (0, 7));
    for &(s, e) in &pieces {
        assert!(!text[s..e].iter().all(|c| c.is_whitespace()));
    }
    let blank: Vec<char> = chars("   ");
    assert!(split_at_boundary(&blank, 0, 3, 8).unwrap().is_empty());
}

#[test]
fn split_span_matches_the_checked_entry_on_valid_inputs() {
    // The chunkers' pre-validated path must be the checked path minus the
    // checks: same pieces on every valid (bounds, budget) combination,
    // including blank spans and budgets past the text length.
    let texts = [
        "aaa bbb\nccc ddd eee".to_owned(),
        "단어 ".repeat(40),
        "   ".to_owned(),
    ];
    for text in &texts {
        let chars = chars(text);
        for &(s, e) in &[
            (0, chars.len()),
            (0, 3.min(chars.len())),
            (2.min(chars.len()), chars.len()),
        ] {
            for budget in [1, 4, 8, 10_000] {
                assert_eq!(
                    split_span(&chars, s, e, budget),
                    split_at_boundary(&chars, s, e, budget).unwrap(),
                    "text={text:?} span=({s}, {e}) budget={budget}",
                );
            }
        }
    }
}

#[test]
fn cap_spans_only_resplits_dense_spans() {
    let text = chars("short מִשְׁפָּטצְדָקָהמִשְׁפָּטצְדָקָה end");
    let all = vec![(0, text.len())];
    let capped = cap_spans(&text, &all, 2).unwrap();
    assert!(capped.len() > 1);
    let kept = cap_spans(&text, &[(0, 5)], 2_000).unwrap();
    assert_eq!(kept, vec![(0, 5)]);
    assert!(cap_spans(&text, &all, 0).is_err());
}

// --- sections ---

#[test]
fn markdown_sections_are_disjoint_and_ordered() {
    let text = "# Title\n\nbody one\n\n## Sub\n\nbody two\n\n# Next\n\ntail\n";
    let sections = sections_from_markdown(text);
    assert_eq!(sections.len(), 3);
    assert_eq!(sections[0].heading, "Title");
    assert_eq!(sections[0].level, 1);
    assert_eq!(sections[1].heading, "Sub");
    assert_eq!(sections[1].level, 2);
    for w in sections.windows(2) {
        assert!(w[0].char_end <= w[1].char_start);
    }
    // Full coverage from the first heading to the end of the text.
    // Sections are trim_spanned, so the last one ends before the trailing newline.
    assert_eq!(sections.last().unwrap().char_end, text.chars().count() - 1);
    assert!(sections_from_markdown("no headings here").is_empty());
}

#[test]
fn char_predicates_hold_at_range_boundaries() {
    // The 10 Python `re \s` / `str.isspace()` ranges, swept exact over every
    // code point in the differential. Boundary probes pin the edges.
    let ws_ranges = [
        (0x09, 0x0D),
        (0x1C, 0x20),
        (0x85, 0x85),
        (0xA0, 0xA0),
        (0x1680, 0x1680),
        (0x2000, 0x200A),
        (0x2028, 0x2029),
        (0x202F, 0x202F),
        (0x205F, 0x205F),
        (0x3000, 0x3000),
    ];
    for (lo, hi) in ws_ranges {
        for cp in lo..=hi {
            assert!(
                marginalia_text::chars::is_space(char::from_u32(cp).unwrap()),
                "U+{cp:04X}"
            );
        }
        if lo > 0 {
            if let Some(c) = char::from_u32(lo - 1) {
                assert!(!marginalia_text::chars::is_space(c), "U+{:04X}", lo - 1);
            }
        }
        if let Some(c) = char::from_u32(hi + 1) {
            if !ws_ranges.iter().any(|&(l, h)| l <= hi + 1 && hi < h) {
                assert!(!marginalia_text::chars::is_space(c), "U+{:04X}", hi + 1);
            }
        }
    }
    // Word-char spots on the classes where property expressions go wrong:
    // `No` numerics count, `M` marks and join controls do not.
    use marginalia_text::word_table::is_word_char;
    for c in ['a', 'Z', '0', '_', '²', 'Ⅷ', 'ﬁ', '𐐀'] {
        assert!(is_word_char(c), "{c:?}");
    }
    for c in [' ', '-', '́', '‌', '“'] {
        assert!(!is_word_char(c), "{c:?}");
    }
    // Baked-table size pins the CPython/Unicode version the sweep verified.
    assert_eq!(marginalia_text::word_table::WORD_RANGES.len(), 749);
}

#[test]
fn linebreak_hyphen_respects_word_membership() {
    // U+3007 is a Python word char (`Nl`) the regex crate excludes, and it
    // survives NFKC: de-hyphenates only with the baked table.
    assert_eq!(normalize("〇-\nx"), "〇x");
    // Combining marks and join controls are not: the hyphen stays.
    assert_eq!(normalize("\u{301}-\nx"), "\u{301}- x");
    assert_eq!(normalize("a\u{200C}-\nx"), "a\u{200C}- x");
}

#[test]
fn split_returns_short_spans_whole() {
    let text: Vec<char> = "hello".chars().collect();
    assert_eq!(split_at_boundary(&text, 0, 5, 8).unwrap(), vec![(0, 5)]);
}

#[test]
fn long_texts_sample_with_a_stride() {
    // Values pinned against CPython (Neumaier over the strided sample).
    let text = "abκ".repeat(25_000);
    assert_eq!(chars_per_token(&text), 2.7391304347826084);
    assert_eq!(min_chars_per_token(&text), 1.68);
    assert_eq!(approx_tokens(&text, None), 27380);
}

#[test]
fn min_rate_early_return() {
    assert_eq!(min_chars_per_token(""), DEFAULT_CHARS_PER_TOKEN);
    assert_eq!(min_chars_per_token("plain ascii"), DEFAULT_CHARS_PER_TOKEN);
}

#[test]
fn exact_tiling_skips_the_remainder_push() {
    let text: Vec<char> = "a".repeat(8).chars().collect();
    assert_eq!(
        split_at_boundary(&text, 0, 8, 4).unwrap(),
        vec![(0, 4), (4, 8)]
    );
}
