//! Phase 1 acceptance: parity with `test_normalize_map.py`,
//! `test_anchoring.py`, `test_chunking.py`, `test_token_estimation.py`,
//! `test_quote_verification.py` (pure core), and the section suites.

use marginalia_text::anchoring::{
    best_overlap, collapse_whitespace, collapse_whitespace_with_map, CanonicalIndex, Span,
};
use marginalia_text::normalize::{
    normalize, normalize_for_matching, normalize_whitespace, normalize_with_map,
    NORMALIZATION_VERSION,
};
use marginalia_text::quote::{
    find_folded, locate_in_window, locate_normalized_windowed, longest_prefix_len,
    verify_against_text, Tier, DIVERGENCE_CONTEXT, NORMALIZED_DETAIL,
};
use marginalia_text::sections::{sections_from_chapters, sections_from_markdown};
use marginalia_text::spans::{cap_spans, split_at_boundary, split_span, trim_span};
use marginalia_text::tokens::{
    approx_tokens, chars_per_token, min_chars_per_token, token_budget_chars, ABSOLUTE_MAX_TOKENS,
    DEFAULT_CHARS_PER_TOKEN,
};
use uuid::Uuid;

fn doc() -> Uuid {
    Uuid::parse_str("11111111-2222-3333-4444-555555555501").unwrap()
}

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

#[test]
fn whitespace_only_collapse() {
    assert_eq!(normalize_whitespace("a  b\n\tc "), "a b c");
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

// --- anchoring ---

#[test]
fn collapse_map_points_at_original_offsets() {
    let (collapsed, map) = collapse_whitespace_with_map("a  b\n\tc");
    assert_eq!(collapsed, "a b c");
    assert_eq!(map, vec![0, 1, 3, 4, 6]);
    assert_eq!(collapse_whitespace("a  b"), "a b");
}

#[test]
fn canonical_index_finds_through_collapsed_whitespace() {
    let raw = "mishpat   and\n\n  tsedaqah";
    let index = CanonicalIndex::new(raw);
    let span = index.find("mishpat and tsedaqah", 0).unwrap();
    assert_eq!(raw_slice(raw, (span.start, span.end)), raw);

    assert!(index.find("absent", 0).is_none());
    assert!(index.find("   ", 0).is_none());
}

#[test]
fn repeated_text_resolves_successively_then_falls_back() {
    let raw = "amen amen amen";
    let index = CanonicalIndex::new(raw);
    let first = index.find("amen", 0).unwrap();
    let second = index.find("amen", first.end).unwrap();
    assert_eq!((first.start, second.start), (0, 5));
    // A hint past the last match falls back to the start.
    let again = index.find("amen", 500).unwrap();
    assert_eq!(again.start, 0);
}

#[test]
fn overlap_picks_greatest_and_breaks_ties_early() {
    let span = Span { start: 10, end: 20 };
    let cands = vec![
        ("a", Span { start: 0, end: 12 }),
        ("b", Span { start: 8, end: 20 }),
        ("c", Span { start: 8, end: 20 }),
    ];
    assert_eq!(best_overlap(&span, &cands), Some("b"));
    assert_eq!(best_overlap(&span, &[] as &[(&str, Span)]), None);
    // Touching spans share nothing.
    let touching = vec![("t", Span { start: 20, end: 30 })];
    assert_eq!(best_overlap(&span, &touching), None);
    assert_eq!(span.overlap(&Span { start: 15, end: 25 }), 5);
    assert_eq!(span.width(), 10);
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
fn chapter_runs_detect_sequences_and_reject_mentions() {
    let pad = "x".repeat(6_000);
    let text = format!("Chapter 1\n{pad}\nChapter 2\n{pad}\nChapter 3\n{pad}\n");
    let sections = sections_from_chapters(&text);
    assert_eq!(sections.len(), 3);
    assert!(sections[0].heading.contains("Chapter 1"));
    assert!(sections.iter().all(|s| s.level == 1));

    // A contents list: ascending but entries a few dozen chars apart.
    let toc = (1..=6)
        .map(|n| format!("Chapter {n}"))
        .collect::<Vec<_>>()
        .join("\n");
    assert!(sections_from_chapters(&toc).is_empty());
    // A sequence that stops early mislabels the rest: better nothing.
    let short = format!(
        "Chapter 1\n{pad}\nChapter 2\n{pad}\nChapter 3\n{}",
        "y".repeat(60_000)
    );
    assert!(sections_from_chapters(&short).is_empty());
}

// --- quote tiers ---

const SOURCE: &str = "The prophets pair two words. He requires “justice and righteousness” of \
    every ruler, a phrase the translations render un-\nevenly, and Amos 5:24 \
    makes it a flood.";

#[test]
fn exact_match_reports_exact_and_the_right_span() {
    let result = verify_against_text(doc(), SOURCE, "a phrase the translations render", 0.5);
    assert_eq!(result.tier, Tier::Exact);
    assert!(result.verified());
    let loc = result.location.unwrap();
    assert_eq!(
        &chars(SOURCE)[loc.char_start..loc.char_end]
            .iter()
            .collect::<String>(),
        "a phrase the translations render"
    );
    assert_eq!(loc.source_text, "a phrase the translations render");
    assert_eq!(result.documents_checked, 1);
}

#[test]
fn typography_reports_normalized_never_exact() {
    for quote in [
        "\"justice and righteousness\"",
        "a phrase the translations render unevenly",
        "He requires \"justice and righteousness\" of every ruler",
    ] {
        let result = verify_against_text(doc(), SOURCE, quote, 0.5);
        assert_eq!(result.tier, Tier::Normalized, "{quote}");
        assert!(result.verified());
        assert_eq!(result.detail, NORMALIZED_DETAIL);
    }
}

#[test]
fn normalized_hit_returns_untouched_source_typography() {
    let result = verify_against_text(doc(), SOURCE, "\"justice and righteousness\"", 0.5);
    let loc = result.location.unwrap();
    assert_eq!(loc.source_text, "“justice and righteousness”");
}

#[test]
fn collapsed_whitespace_alone_is_still_not_exact() {
    let raw = "He paused   and then continued.";
    let result = verify_against_text(doc(), raw, "He paused and then continued.", 0.5);
    assert_eq!(result.tier, Tier::Normalized);
}

#[test]
fn absent_and_empty_quotes_are_not_found() {
    let result = verify_against_text(doc(), SOURCE, "no such words here", 0.5);
    assert_eq!(result.tier, Tier::NotFound);
    assert!(!result.verified());
    assert!(result.divergence.is_none());

    let result = verify_against_text(doc(), SOURCE, "   ", 0.5);
    assert_eq!(result.tier, Tier::NotFound);
    assert_eq!(result.matched_fraction, None);
    assert_eq!(result.detail, "The quotation is empty.");
}

#[test]
fn wrong_ending_reports_where_it_diverges() {
    // Mirrors the Python near-miss suite case for case.
    let result = verify_against_text(
        doc(),
        SOURCE,
        "of every ruler, a phrase the translations render unevenly, and the moon",
        0.5,
    );
    assert_eq!(result.tier, Tier::Near);
    assert!(!result.verified());
    let fraction = result.matched_fraction.unwrap();
    assert!((0.5..1.0).contains(&fraction));
    let div = result.divergence.unwrap();
    assert!(div.quote_continues.contains("moon"));
    assert!(div.matched_characters > 0);
    // The source's continuation at the divergence, not the quote's own tail.
    assert_eq!(div.source_continues, "Amos 5:24 makes it a flood.");
    // The landing span is reported too.
    assert!(result.location.is_some());
}

#[test]
fn trivial_overlap_is_not_a_near_miss() {
    let result = verify_against_text(doc(), SOURCE, "xyzzy the fog", 0.5);
    assert_eq!(result.tier, Tier::NotFound);
}

#[test]
fn window_locate_rebases_to_document_offsets() {
    let raw: Vec<char> = SOURCE.chars().collect();
    let at: usize = SOURCE.find("Amos 5:24").unwrap();
    let at = SOURCE[..at].chars().count();
    let found = locate_in_window(
        SOURCE,
        (at, at + 9),
        "Amos 5:24",
        &marginalia_text::normalize::normalize_for_matching("Amos 5:24"),
    )
    .unwrap();
    assert_eq!(found.0, Tier::Exact);
    assert_eq!(
        &raw[found.1.start..found.1.end].iter().collect::<String>(),
        "Amos 5:24"
    );

    assert!(
        locate_in_window(SOURCE, (0, 10), "Amos 5:24 far away", "amos 5:24 far away").is_none()
    );
}

#[test]
fn windowed_folded_locate_matches_plain_fold() {
    let raw = "He requires “justice” of all.";
    let match_form = marginalia_text::normalize::normalize_for_matching("\"justice\"");
    let norm_offset = marginalia_text::normalize::normalize(raw)
        .find(&marginalia_text::normalize::normalize("\"justice\""))
        .map(|b| raw[..b].chars().count())
        .unwrap();
    let raw_len = raw.chars().count();
    let norm_len = marginalia_text::normalize::normalize(raw).chars().count();
    let span =
        locate_normalized_windowed(raw, norm_offset, raw_len, norm_len, &match_form).unwrap();
    let direct = find_folded(raw, &match_form).unwrap();
    assert_eq!((span.start, span.end), (direct.start, direct.end));
}

#[test]
fn longest_prefix_binary_searches_probes() {
    let doc = "the quick brown fox jumps";
    let len = longest_prefix_len("the quick brown fox jumps over", &|p| doc.contains(p));
    assert_eq!(len, "the quick brown fox jumps".chars().count());
    // Whitespace-only probes never count.
    assert_eq!(longest_prefix_len("   ", &|_| true), 0);
    assert_eq!(DIVERGENCE_CONTEXT, 80);
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
fn folded_search_helpers_cover_edges() {
    assert_eq!(find_folded("abc", ""), None);
    assert_eq!(find_folded("ab", "abcdef"), None);
    let loc = marginalia_text::quote::QuoteLocation {
        document_id: doc(),
        document_title: None,
        char_start: 0,
        char_end: 3,
        source_text: "abc".to_owned(),
        passage_ids: vec![doc()],
        locators: vec![],
        node: None,
    };
    assert!(!loc.straddles_passages());
    let loc2 = marginalia_text::quote::QuoteLocation {
        passage_ids: vec![doc(), doc()],
        ..loc
    };
    assert!(loc2.straddles_passages());
}

#[test]
fn window_locate_reports_folded_hits_rebased() {
    let match_form =
        marginalia_text::normalize::normalize_for_matching("\"justice and righteousness\"");
    let at = SOURCE.find("justice").unwrap();
    let at = SOURCE[..at].chars().count();
    let quote = "\"justice and righteousness\"";
    let (tier, span) = locate_in_window(SOURCE, (at - 1, at + 30), quote, &match_form).unwrap();
    assert_eq!(tier, Tier::Normalized);
    assert_eq!(
        &chars(SOURCE)[span.start..span.end]
            .iter()
            .collect::<String>(),
        "\u{201C}justice and righteousness\u{201D}"
    );
}
#[test]
fn windowed_locate_widens_then_falls_back() {
    // Skewed lengths: the ratio estimate misses the first window (4 KiB) and
    // hits the second (64 KiB).
    let raw = format!("{}{}", " ".repeat(20_000), "target quote here");
    let match_form = "target quote here";
    let span = locate_normalized_windowed(&raw, 0, raw.chars().count(), 17, match_form).unwrap();
    assert_eq!(
        &chars(&raw)[span.start..span.end].iter().collect::<String>(),
        match_form
    );
    // A small document whose first window is already everything: one probe.
    assert_eq!(locate_normalized_windowed("abc", 0, 3, 3, "zzz"), None);
    // A lying length ratio misses every window; the whole-document fallback
    // still finds the match.
    let raw = format!("target{}", "x".repeat(2_000_000));
    let span = locate_normalized_windowed(
        &raw,
        1_900_000,
        raw.chars().count(),
        raw.chars().count(),
        "target",
    )
    .unwrap();
    assert_eq!((span.start, span.end), (0, 6));
}

#[test]
fn folding_empty_quote_is_not_found_without_fraction() {
    // Soft-hyphen-only: non-blank, but folds to nothing.
    let result = verify_against_text(doc(), SOURCE, "\u{AD}", 0.5);
    assert_eq!(result.tier, Tier::NotFound);
    assert_eq!(result.matched_fraction, None);
}

#[test]
fn composed_quote_against_decomposed_source_diverges() {
    // Stored form (composed) is found in the normalized column, but the
    // per-character fold never matches: near miss with no landing span.
    let result = verify_against_text(doc(), "xe\u{301}y", "xéy", 0.5);
    assert_eq!(result.tier, Tier::Near);
    assert_eq!(result.location, None);
    assert_eq!(result.divergence.unwrap().matched_characters, 3);
}

#[test]
fn zero_threshold_reports_empty_prefix_as_near() {
    let result = verify_against_text(doc(), SOURCE, "zzz absent", 0.0);
    assert_eq!(result.tier, Tier::Near);
    assert_eq!(result.location, None);
    assert_eq!(result.matched_fraction, Some(0.0));
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
fn chapter_numbers_cover_words_roman_and_rejects() {
    let pad = "x".repeat(6_000);
    let words = format!("Chapter One\n{pad}\nChapter Two\n{pad}\nChapter Three\n{pad}\n");
    let sections = sections_from_chapters(&words);
    assert_eq!(sections.len(), 3);
    assert!(sections[0].heading.contains("One"));
    let roman = format!("Chapter IV\n{pad}\nChapter V\n{pad}\nChapter VI\n{pad}\n");
    assert_eq!(sections_from_chapters(&roman).len(), 3);
    // Unparseable tokens are skipped, leaving no sequence.
    let bad = format!("Chapter Banana\n{pad}\nChapter Apple\n{pad}\nChapter Pear\n{pad}\n");
    assert!(sections_from_chapters(&bad).is_empty());
    // Overlong lines are mentions, not starts.
    let long = format!(
        "Chapter 1 {ys}\n{pad}\nChapter 2\n{pad}\nChapter 3\n{pad}",
        ys = "y".repeat(100)
    );
    assert!(sections_from_chapters(&long).is_empty());
    // A descending pair splits into runs too short to count.
    let split = format!("Chapter 2\n{pad}\nChapter 1\n{pad}\n");
    assert!(sections_from_chapters(&split).is_empty());
}

#[test]
fn widest_run_wins_over_longest() {
    // Two qualifying sequences force the widest-span sort to compare.
    let pad = "x".repeat(6_000);
    let first = (1..=3)
        .map(|n| format!("Chapter {n}\n{pad}\n"))
        .collect::<String>();
    let second = (1..=4)
        .map(|n| format!("Chapter {n}\n{pad}\n"))
        .collect::<String>();
    let text = format!("{first}{second}");
    let sections = sections_from_chapters(&text);
    assert_eq!(sections.len(), 4);
    assert!(sections[3].heading.contains('4'));
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
