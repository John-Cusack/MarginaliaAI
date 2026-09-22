//! Phase 2 acceptance: parity with `test_chunking.py`,
//! `test_chunker_contract.py`, `test_structural_chunker.py`, `test_fusion.py`,
//! `test_search_windows.py`, and the `langconfig` contract.

use chrono::Utc;
use marginalia_chunk::fixed_window::{
    FixedWindowChunker, DEFAULT_OVERLAP_CHARS, DEFAULT_WINDOW_CHARS,
};
use marginalia_chunk::fusion::{rrf_fuse, weighted_fuse, RRF_K};
use marginalia_chunk::langconfig::{is_known_config, pg_config, DEFAULT_CONFIG};
use marginalia_chunk::prose_window::{sentence_spans, ProseWindowChunker};
use marginalia_chunk::structural::{SectionInput, StructuralChunker};
use marginalia_chunk::whole_or_paragraph::{
    paragraph_spans, WholeOrParagraphChunker, CEILING_TOKENS,
};
use marginalia_chunk::windows::{build_window, choose_window, window_budgets};
use marginalia_text::anchoring::Span;
use marginalia_types::nodes::DocumentNode;
use marginalia_types::passages::WindowSource;
use marginalia_types::sdk::PassageDraft;
use serde_json::{Map, Value};
use uuid::Uuid;

fn chars(s: &str) -> Vec<char> {
    s.chars().collect()
}

fn slice(text: &str, start: i64, end: i64) -> String {
    chars(text)[start as usize..end as usize].iter().collect()
}

/// The chunker contract: every draft's span addresses non-empty text it
/// carries, and the model validator agrees. Non-emptiness is the load-bearing
/// half: the chunkers never emit blank drafts, which is what lets the prose
/// tail push its last window unconditionally.
fn assert_offsets_are_true(text: &str, drafts: &[PassageDraft]) {
    assert!(
        !drafts.is_empty(),
        "a chunker returned no drafts for {text:?}"
    );
    for d in drafts {
        assert!(!d.text.is_empty(), "chunker emitted a blank draft");
        assert_eq!(
            slice(text, d.char_start, d.char_end),
            d.text,
            "span [{}, {}) does not address its text",
            d.char_start,
            d.char_end
        );
        d.validate().expect("draft fails its own span validator");
    }
}

fn positions_sequential(drafts: &[PassageDraft]) {
    let want: Vec<i64> = (0..drafts.len() as i64).collect();
    let got: Vec<i64> = drafts.iter().map(|d| d.position).collect();
    assert_eq!(got, want);
}

// --- fixed_window ---

#[test]
fn fixed_window_short_text_is_one_chunk() {
    let drafts = FixedWindowChunker::default().chunk("Short text.", None);
    assert_eq!(drafts.len(), 1);
    assert_eq!(drafts[0].text, "Short text.");
    assert_offsets_are_true("Short text.", &drafts);
}

#[test]
fn fixed_window_walks_with_overlap() {
    let text = "A".repeat(5000);
    let drafts =
        FixedWindowChunker::new(DEFAULT_WINDOW_CHARS, DEFAULT_OVERLAP_CHARS).chunk(&text, None);
    assert!(drafts.len() >= 2);
    positions_sequential(&drafts);
    assert_offsets_are_true(&text, &drafts);
    // Overlap: the second chunk starts before the first ends.
    assert!(drafts[1].char_start < drafts[0].char_end);
}

#[test]
fn fixed_window_blank_is_empty() {
    assert!(FixedWindowChunker::default().chunk("", None).is_empty());
    assert!(FixedWindowChunker::default()
        .chunk("   \n\t  ", None)
        .is_empty());
}

#[test]
fn fixed_window_scales_cjk_to_the_same_token_budget() {
    // 2000 chars of English == 500 tokens; CJK at 1.5 chars/token must hold
    // the same budget in ~750 characters.
    let cjk = "字".repeat(5000);
    let drafts = FixedWindowChunker::default().chunk(&cjk, None);
    assert!(drafts.len() >= 2);
    let first_width = drafts[0].char_end - drafts[0].char_start;
    assert!(
        (700..=800).contains(&first_width),
        "CJK window holds {first_width} chars, not ~750"
    );
    assert_offsets_are_true(&cjk, &drafts);
}

#[test]
fn fixed_window_version_is_not_1x() {
    assert_ne!(marginalia_chunk::fixed_window::VERSION, "1.0");
    assert_eq!(FixedWindowChunker::default().max_passage_tokens(), 500);
}

// --- prose_window ---

#[test]
fn prose_window_short_text_is_one_chunk() {
    let drafts = ProseWindowChunker::new(500, 50)
        .chunk("This is a short sentence.", None)
        .unwrap();
    assert_eq!(drafts.len(), 1);
    assert_eq!(drafts[0].text, "This is a short sentence.");
}

#[test]
fn prose_window_splits_long_text_with_sequential_positions() {
    let text = (0..100)
        .map(|i| format!("Sentence number {i} with some extra words to fill space"))
        .collect::<Vec<_>>()
        .join(". ");
    let drafts = ProseWindowChunker::new(100, 20).chunk(&text, None).unwrap();
    assert!(drafts.len() > 1);
    positions_sequential(&drafts);
    assert_offsets_are_true(&text, &drafts);
}

#[test]
fn prose_window_empty_and_metadata() {
    assert!(ProseWindowChunker::default()
        .chunk("", None)
        .unwrap()
        .is_empty());
    let mut meta = Map::new();
    meta.insert("source".to_owned(), Value::String("test".to_owned()));
    let drafts = ProseWindowChunker::default()
        .chunk("Test text.", Some(&meta))
        .unwrap();
    assert_eq!(drafts[0].metadata, meta);
}

#[test]
fn prose_window_preserves_paragraph_structure() {
    // The 1.0 damage: `" ".join(sentences)` collapsed every whitespace run.
    // Slicing keeps the blank line inside the chunk.
    let text = "First para line.\n\nSecond para line. Third sentence.";
    let drafts = ProseWindowChunker::default().chunk(text, None).unwrap();
    assert_eq!(drafts.len(), 1);
    assert!(drafts[0].text.contains("\n\n"));
    assert_offsets_are_true(text, &drafts);
}

#[test]
fn sentence_spans_split_at_capital_starts_only() {
    assert_eq!(
        sentence_spans("One. Two. Three."),
        vec![(0, 4), (5, 9), (10, 16)]
    );
    // No capital after the run: one span (e.g. "no. 5" ordinals, lowercase).
    assert_eq!(sentence_spans("item no. 5 costs less"), vec![(0, 21)]);
    // `!` and `?` are boundaries too.
    assert_eq!(
        sentence_spans("Really? Yes! Done."),
        vec![(0, 7), (8, 12), (13, 18)]
    );
    // File/group separators count as whitespace, exactly as Python `\s` does.
    assert_eq!(sentence_spans("One.\u{1c}Two."), vec![(0, 4), (5, 9)]);
}

#[test]
fn prose_window_version_is_not_1x() {
    assert_ne!(marginalia_chunk::prose_window::VERSION, "1.0");
}

// --- whole_or_paragraph ---

#[test]
fn whole_or_paragraph_short_text_stays_whole() {
    let drafts = WholeOrParagraphChunker::new(1000)
        .chunk("Short text here.", None)
        .unwrap();
    assert_eq!(drafts.len(), 1);
    assert_eq!(drafts[0].text, "Short text here.");
}

#[test]
fn whole_or_paragraph_splits_on_blank_lines() {
    let text = "Paragraph one with some content.\n\nParagraph two with different content.\n\nParagraph three.";
    let drafts = WholeOrParagraphChunker::new(5).chunk(text, None).unwrap();
    assert_eq!(drafts.len(), 3);
    assert_offsets_are_true(text, &drafts);
}

#[test]
fn whole_or_paragraph_drops_blank_paragraphs() {
    let text = "Para one.\n\n\n\nPara two.";
    let drafts = WholeOrParagraphChunker::new(2).chunk(text, None).unwrap();
    assert_eq!(drafts.len(), 2);
    assert_offsets_are_true(text, &drafts);
}

#[test]
fn whole_or_paragraph_breaks_a_past_ceiling_paragraph() {
    // One 22k-token "paragraph" must not be emitted whole: most of it would
    // be stored and never embedded.
    let text = "word ".repeat(CEILING_TOKENS as usize * 4);
    let drafts = WholeOrParagraphChunker::default()
        .chunk(&text, None)
        .unwrap();
    assert!(drafts.len() > 1);
    assert_offsets_are_true(&text, &drafts);
}

#[test]
fn paragraph_spans_use_blank_line_boundaries() {
    assert_eq!(paragraph_spans("a\n\nb\n\nc"), vec![(0, 1), (3, 4), (6, 7)]);
    assert_eq!(paragraph_spans("a\nb"), vec![(0, 3)]);
    assert!(paragraph_spans("  \n\n  ").is_empty());
}

#[test]
fn whole_or_paragraph_is_unbounded_by_design() {
    assert_eq!(
        WholeOrParagraphChunker::default().max_passage_tokens(),
        None
    );
}

// --- structural ---

fn build_document(bodies: &[&str]) -> (String, Vec<SectionInput>) {
    let mut text = String::new();
    let mut sections = Vec::new();
    for (i, body) in bodies.iter().enumerate() {
        let start = text.chars().count();
        text.push_str(body);
        let end = text.chars().count();
        sections.push(SectionInput {
            text: Some((*body).to_owned()),
            heading: Some(format!("Heading {i}")),
            level: Some(1),
            page: None,
            char_start: Some(start as i64),
            char_end: Some(end as i64),
        });
    }
    (text, sections)
}

#[test]
fn structural_one_section_is_one_passage() {
    let (text, sections) = build_document(&["Short body."]);
    let drafts = StructuralChunker::default()
        .chunk(&sections, None, Some(&text))
        .unwrap();
    assert_eq!(drafts.len(), 1);
    assert_eq!(
        drafts[0].locator["heading"],
        Value::String("Heading 0".to_owned())
    );
    assert_eq!(drafts[0].text, text);
    assert_offsets_are_true(&text, &drafts);
}

#[test]
fn structural_windows_an_oversized_section() {
    let long: String = ["The clerk recorded every detail of the transaction. "; 10].join("");
    let long = long.trim().to_owned();
    let (text, sections) = build_document(&[&long]);
    let drafts = StructuralChunker::new(60, 5)
        .chunk(&sections, None, Some(&text))
        .unwrap();
    assert!(drafts.len() > 1);
    assert!(drafts.iter().all(|d| d.chunker == "structural"));
    // The heading travels with every piece.
    assert!(drafts
        .iter()
        .all(|d| d.locator["heading"] == Value::String("Heading 0".to_owned())));
    let parts: Vec<i64> = drafts
        .iter()
        .map(|d| d.locator["section_part"].as_i64().unwrap())
        .collect();
    assert_eq!(parts, (1..=drafts.len() as i64).collect::<Vec<_>>());
    assert!(drafts
        .iter()
        .all(|d| d.locator["section_parts"] == Value::from(drafts.len() as i64).as_i64().unwrap()));
    positions_sequential(&drafts);
    let starts: Vec<i64> = drafts.iter().map(|d| d.char_start).collect();
    assert_eq!(starts, {
        let mut s = starts.clone();
        s.sort();
        s
    });
    assert_offsets_are_true(&text, &drafts);
}

#[test]
fn structural_locates_repeated_sections_successively() {
    let body = "Short body. ";
    let text = body.repeat(3);
    let sections: Vec<SectionInput> = (0..3)
        .map(|i| SectionInput {
            text: Some(body.to_owned()),
            heading: Some(format!("H{i}")),
            level: Some(1),
            ..Default::default()
        })
        .collect();
    let drafts = StructuralChunker::default()
        .chunk(&sections, None, Some(&text))
        .unwrap();
    assert_eq!(drafts.len(), 3);
    let starts: Vec<i64> = drafts.iter().map(|d| d.char_start).collect();
    assert_eq!(starts, {
        let mut s = starts.clone();
        s.sort();
        s
    });
    assert_eq!(starts.len(), 3);
    assert!(starts[0] < starts[1] && starts[1] < starts[2]);
    assert_offsets_are_true(&text, &drafts);
}

#[test]
fn structural_reads_boundary_only_sections_from_full_text() {
    let (text, sections) = build_document(&["Short body."]);
    let boundaries: Vec<SectionInput> = sections
        .into_iter()
        .map(|mut s| {
            s.text = None;
            s
        })
        .collect();
    let drafts = StructuralChunker::default()
        .chunk(&boundaries, None, Some(&text))
        .unwrap();
    assert_eq!(drafts[0].text, text);
    assert_offsets_are_true(&text, &drafts);
}

#[test]
fn structural_refuses_homeless_and_mismatched_sections() {
    let homeless = vec![SectionInput {
        text: Some("Body without a home.".to_owned()),
        ..Default::default()
    }];
    let err = StructuralChunker::default()
        .chunk(&homeless, None, None)
        .unwrap_err();
    assert!(err.to_string().contains("offsets"), "unexpected: {err}");

    let (text, mut sections) = build_document(&["Actual body here."]);
    sections[0].text = Some("Different words.".to_owned());
    let err = StructuralChunker::default()
        .chunk(&sections, None, Some(&text))
        .unwrap_err();
    assert!(
        err.to_string().contains("does not match"),
        "unexpected: {err}"
    );
}

#[test]
fn structural_caps_a_dense_section_at_its_own_density() {
    // A Greek section inside an English book: judged by the book's rate it
    // reads as ~English and slips a 947-token section past a 500 cap.
    let english = "The clerk recorded the transaction in the ledger. ".repeat(400);
    let greek = "λόγος πρὸς τὸν θεόν καὶ θεὸς ἦν ὁ λόγος οὗτος ἦν ἐν ἀρχῇ ".repeat(22);
    let text = format!("{english}{greek}");
    let sections = vec![
        SectionInput {
            char_start: Some(0),
            char_end: Some(english.chars().count() as i64),
            level: Some(1),
            ..Default::default()
        },
        SectionInput {
            char_start: Some(english.chars().count() as i64),
            char_end: Some(text.chars().count() as i64),
            level: Some(1),
            ..Default::default()
        },
    ];
    let chunker = StructuralChunker::default();
    let drafts = chunker.chunk(&sections, None, Some(&text)).unwrap();
    let independent = |chunk: &str| {
        let dense = chunk.chars().filter(|c| *c as u32 >= 128).count();
        ((chunk.chars().count() - dense) as f64 / 4.0 + dense as f64 / 1.5) as i64
    };
    let worst = drafts.iter().map(|d| independent(&d.text)).max().unwrap();
    let cap = chunker.max_passage_tokens().unwrap();
    assert!(
        worst <= (cap as f64 * 1.5) as i64,
        "emitted a {worst}-token passage against a {cap} cap"
    );
    assert_offsets_are_true(&text, &drafts);
}

// --- fusion ---

fn pid(n: u128) -> Uuid {
    Uuid::from_u128(n)
}

#[test]
fn rrf_orders_by_rank_and_reports_breakdown() {
    let (a, b) = (pid(1), pid(2));
    let out = rrf_fuse(&[vec![(a, 0.9), (b, 0.7)]]);
    assert_eq!(out.len(), 2);
    assert_eq!(out[0].0, a);
    assert!(out[0].1 > out[1].1);
    assert!((out[0].1 - 1.0 / (RRF_K + 1.0)).abs() < 1e-12);
}

#[test]
fn rrf_top_in_both_lists_wins() {
    let (a, b, c) = (pid(1), pid(2), pid(3));
    let out = rrf_fuse(&[
        vec![(a, 0.9), (b, 0.8)],
        vec![(a, 0.9), (c, 0.85), (b, 0.4)],
    ]);
    assert_eq!(out[0].0, a);
    let (_, _, breakdown) = &out[0];
    assert_eq!(breakdown["list_0"].rank, 1);
    assert_eq!(breakdown["list_1"].rank, 1);
    assert_eq!(breakdown["list_0"].score, 0.9);
}

#[test]
fn rrf_empty_and_tie_order() {
    assert!(rrf_fuse(&[vec![], vec![]]).is_empty());
    // Rank 1 in different lists: equal scores, first-seen order kept.
    let (a, b) = (pid(1), pid(2));
    let out = rrf_fuse(&[vec![(a, 0.9)], vec![(b, 0.8)]]);
    assert!((out[0].1 - out[1].1).abs() < 0.01);
    assert_eq!(out[0].0, a);
}

#[test]
fn weighted_fuse_min_max_math_and_alpha() {
    let (a, b) = (pid(1), pid(2));
    // vec scores 10/20 -> norms 0/1; kw scores 5/15 -> norms 0/1.
    let out = weighted_fuse(&[(a, 10.0), (b, 20.0)], &[(a, 5.0), (b, 15.0)], 0.5);
    assert_eq!(out[0].0, b);
    assert!((out[0].1 - 1.0).abs() < 1e-12);
    assert!((out[1].1 - 0.0).abs() < 1e-12);

    // alpha=1 ignores keyword entirely.
    let out = weighted_fuse(&[(a, 1.0), (b, 2.0)], &[(a, 100.0)], 1.0);
    assert_eq!(out[0].0, b);
    assert!((out[0].2.vector_norm - 1.0).abs() < 1e-12);
    assert!((out[0].2.keyword_norm - 0.0).abs() < 1e-12);
}

#[test]
fn weighted_fuse_empty_lists() {
    assert!(weighted_fuse(&[], &[], 0.5).is_empty());
    let (a,) = (pid(1),);
    let out = weighted_fuse(&[], &[(a, 3.0)], 0.5);
    assert_eq!(out.len(), 1);
    // Single-score list normalizes to 0 (range fallback), weighted by 0.5.
    assert!((out[0].1 - 0.0).abs() < 1e-12);
}

// --- langconfig ---

#[test]
fn langconfig_maps_and_falls_back() {
    assert_eq!(pg_config(Some("de")), "german");
    assert_eq!(pg_config(Some("de-CH")), "german");
    assert_eq!(pg_config(Some("EN")), "english");
    assert_eq!(pg_config(Some("el")), "greek");
    assert_eq!(pg_config(None), DEFAULT_CONFIG);
    assert_eq!(pg_config(Some("xx")), DEFAULT_CONFIG);
    assert_eq!(pg_config(Some("")), DEFAULT_CONFIG);
    assert_eq!(DEFAULT_CONFIG, "simple");
}

#[test]
fn langconfig_guards_sql_interpolation() {
    assert!(is_known_config("german"));
    assert!(is_known_config("simple"));
    assert!(!is_known_config("english; DROP TABLE passages"));
    assert!(!is_known_config("ENGLISH"));
}

// --- windows ---

fn doc_node(start: i64, end: i64, depth: i64, title: &str) -> DocumentNode {
    DocumentNode {
        id: Uuid::new_v4(),
        document_id: Uuid::nil(),
        parent_id: None,
        path: format!("r{}", ".n0".repeat(depth as usize)),
        depth,
        position: 0,
        node_type: "section".to_owned(),
        title: Some(title.to_owned()),
        char_start: start,
        char_end: end,
        metadata: Map::new(),
        created_at: Utc::now(),
    }
}

#[test]
fn window_never_narrower_than_chunk_and_names_node() {
    let root = doc_node(0, 200_000, 0, "Louw-Nida");
    let domain = doc_node(1_000, 1_400, 1, "Domain 56: Justice");
    let entry = doc_node(1_200, 1_268, 2, "entry");
    let chain = vec![root.clone(), domain.clone(), entry.clone()];
    let passage = Span {
        start: 1_190,
        end: 1_290,
    };

    let plan = choose_window(Some(passage), &chain, 6_000, 800).unwrap();
    assert!(plan.span.start <= passage.start);
    assert!(plan.span.end >= passage.end);
    // Climbs past the too-small domain to the root, widened to the budget.
    assert_eq!(plan.node_id, Some(root.id));
    assert_eq!(plan.source, WindowSource::NodeWindow);
    assert!((plan.span.start, plan.span.end) == (0, 6_000));
}

#[test]
fn window_returns_node_when_it_fits_and_matters() {
    let root = doc_node(0, 200_000, 0, "Louw-Nida");
    let domain = doc_node(1_000, 1_400, 1, "Domain 56: Justice");
    let entry = doc_node(1_200, 1_268, 2, "entry");
    let chain = vec![root, domain.clone(), entry];
    let passage = Span {
        start: 1_190,
        end: 1_290,
    };

    let plan = choose_window(Some(passage), &chain, 6_000, 200).unwrap();
    assert_eq!(plan.source, WindowSource::Node);
    assert_eq!((plan.span.start, plan.span.end), (1_000, 1_400));
}

#[test]
fn window_clips_to_narrowest_when_everything_is_too_big() {
    let root = doc_node(0, 23_198_553, 0, "TDNT");
    let plan = choose_window(
        Some(Span {
            start: 500_000,
            end: 501_000,
        }),
        std::slice::from_ref(&root),
        6_000,
        800,
    )
    .unwrap();
    assert!(plan.span.width() <= 6_000);
    assert_eq!(plan.node_id, Some(root.id));
}

#[test]
fn window_without_ancestors_is_a_document_window() {
    let plan = choose_window(
        Some(Span {
            start: 5_000,
            end: 5_200,
        }),
        &[],
        6_000,
        800,
    )
    .unwrap();
    assert_eq!(plan.source, WindowSource::DocumentWindow);
    assert_eq!(plan.node_id, None);
    assert!(plan.span.width() <= 6_000);
}

#[test]
fn window_clamps_at_zero_and_reports_passage_when_budget_is_tiny() {
    let plan = choose_window(Some(Span { start: 10, end: 60 }), &[], 6_000, 800).unwrap();
    assert_eq!(plan.span.start, 0);

    let passage = Span {
        start: 1_000,
        end: 9_000,
    };
    let chain = vec![doc_node(0, 50_000, 0, "doc")];
    let plan = choose_window(Some(passage), &chain, 500, 200).unwrap();
    assert_eq!(
        (plan.span.start, plan.span.end),
        (passage.start, passage.end)
    );
    assert_eq!(plan.source, WindowSource::Passage);
}

#[test]
fn window_none_passage_has_no_window() {
    assert_eq!(choose_window(None, &[], 6_000, 800), None);
}

#[test]
fn window_slides_rather_than_truncates() {
    let chain = vec![doc_node(0, 200_000, 0, "root")];
    let plan = choose_window(
        Some(Span {
            start: 100_010,
            end: 100_100,
        }),
        &chain,
        6_000,
        800,
    )
    .unwrap();
    assert_eq!(plan.span.width(), 6_000);
}

#[test]
fn window_budgets_come_from_the_hit_text() {
    let (budget, min) = window_budgets("Some English hit text here.", 100, 20);
    assert_eq!((budget, min), (400, 80));
    assert_eq!(window_budgets("", 100, 20), (400, 80));
}

#[test]
fn build_window_trims_and_floors() {
    let chain = vec![doc_node(0, 1_000, 0, "Chapter")];
    let passage = Span {
        start: 100,
        end: 200,
    };
    let plan = choose_window(Some(passage), &chain, 600, 50).unwrap();
    assert_eq!((plan.span.start, plan.span.end), (0, 600));
    // A 1000-char canonical: whitespace, a 300-char run holding the passage,
    // whitespace. The fetched slice is what the reader hands back.
    let doc = format!("{}{}{}", " ".repeat(50), "p".repeat(300), " ".repeat(650));
    assert_eq!(doc.chars().count(), 1_000);
    let fetched: String = chars(&doc)[plan.span.start..plan.span.end].iter().collect();
    let window = build_window(Some(passage), &plan, &chain, Some(&fetched)).unwrap();
    assert!(window.char_start <= passage.start as i64);
    assert!(window.char_end >= passage.end as i64);
    assert_eq!(window.text, slice(&doc, window.char_start, window.char_end));
    assert!(!window.text.starts_with(' '));
    assert_eq!(window.breadcrumb, vec!["Chapter".to_owned()]);
    assert_eq!(window.node_id, plan.node_id);

    assert_eq!(build_window(Some(passage), &plan, &chain, None), None);
    assert_eq!(build_window(Some(passage), &plan, &chain, Some("")), None);
}
// --- identities: ids, versions, budgets ---

#[test]
fn chunker_identities_match_the_python_class_attrs() {
    use marginalia_chunk::{fixed_window, prose_window, structural, whole_or_paragraph};
    assert_eq!(fixed_window::ID, "fixed_window");
    assert_eq!(fixed_window::CONSUMES, "text");
    assert_eq!(fixed_window::VERSION, "3.0");
    assert_eq!(prose_window::ID, "prose_window");
    assert_eq!(prose_window::CONSUMES, "text");
    assert_eq!(prose_window::VERSION, "4.0");
    assert_eq!(structural::ID, "structural");
    assert_eq!(structural::CONSUMES, "sections");
    assert_eq!(structural::VERSION, "4.0");
    assert_eq!(whole_or_paragraph::ID, "whole_or_paragraph");
    assert_eq!(whole_or_paragraph::CONSUMES, "text");
    assert_eq!(whole_or_paragraph::VERSION, "4.0");
    assert_eq!(whole_or_paragraph::CEILING_TOKENS, 2000);

    assert_eq!(FixedWindowChunker::default().max_passage_tokens(), 500);
    assert_eq!(
        ProseWindowChunker::default().max_passage_tokens(),
        Some(500)
    );
    assert_eq!(StructuralChunker::default().max_passage_tokens(), Some(500));
    assert_eq!(
        WholeOrParagraphChunker::default().max_passage_tokens(),
        None
    );
}

// --- fixed_window edges ---

#[test]
fn fixed_window_skips_whitespace_only_windows() {
    let text = "abcdefghij          klmnopqrstuvwxyz012345";
    let drafts = FixedWindowChunker::new(10, 0).chunk(text, None);
    assert!(drafts
        .iter()
        .all(|d| !d.text.chars().all(char::is_whitespace)));
    positions_sequential(&drafts);
    assert_offsets_are_true(text, &drafts);
}

#[test]
fn fixed_window_overlap_past_window_still_advances() {
    // `max(end - overlap, start + 1)`: an overlap >= window cannot stall.
    let text = "abcdefghijklmnopqrstuvwxyz";
    let drafts = FixedWindowChunker::new(10, 50).chunk(text, None);
    assert!(drafts.len() > 2);
    positions_sequential(&drafts);
    assert_offsets_are_true(text, &drafts);
}

// --- whole_or_paragraph edges ---

#[test]
fn whole_or_paragraph_blank_is_empty() {
    assert!(WholeOrParagraphChunker::default()
        .chunk("", None)
        .unwrap()
        .is_empty());
    assert!(WholeOrParagraphChunker::default()
        .chunk("  \n\n \t ", None)
        .unwrap()
        .is_empty());
}

// --- structural edges ---

#[test]
fn structural_skips_blank_sections_without_error() {
    let sections = vec![
        SectionInput {
            text: Some("   \n  ".to_owned()),
            heading: Some("Blank".to_owned()),
            ..Default::default()
        },
        SectionInput {
            text: None,
            ..Default::default()
        },
        SectionInput {
            text: Some("Real body.".to_owned()),
            char_start: Some(0),
            char_end: Some(10),
            ..Default::default()
        },
    ];
    // No offsets and no full text: the homeless sections are blank, so they
    // are skipped rather than refused — exactly as upstream.
    let drafts = StructuralChunker::default()
        .chunk(&sections, None, None)
        .unwrap();
    assert_eq!(drafts.len(), 1);
    assert_eq!(drafts[0].text, "Real body.");
}

#[test]
fn structural_omits_falsy_locator_values() {
    let sections = vec![
        SectionInput {
            text: Some("A.".to_owned()),
            heading: Some(String::new()),
            level: Some(0),
            page: Some(Value::Null),
            ..Default::default()
        },
        SectionInput {
            text: Some("B.".to_owned()),
            page: Some(Value::from(0)),
            ..Default::default()
        },
        SectionInput {
            text: Some("C.".to_owned()),
            page: Some(Value::String(String::new())),
            ..Default::default()
        },
        SectionInput {
            text: Some("D.".to_owned()),
            page: Some(Value::Bool(false)),
            ..Default::default()
        },
        SectionInput {
            text: Some("E.".to_owned()),
            page: Some(Value::Array(vec![])),
            ..Default::default()
        },
        SectionInput {
            text: Some("F.".to_owned()),
            page: Some(Value::Object(Map::new())),
            ..Default::default()
        },
        SectionInput {
            text: Some("G.".to_owned()),
            heading: Some("Kept".to_owned()),
            level: Some(2),
            page: Some(Value::from(7)),
            ..Default::default()
        },
        SectionInput {
            text: Some("H.".to_owned()),
            page: Some(Value::String("xiv".to_owned())),
            ..Default::default()
        },
    ];
    let text = "A.B.C.D.E.F.G.H.";
    let drafts = StructuralChunker::default()
        .chunk(&sections, None, Some(text))
        .unwrap();
    assert_eq!(drafts.len(), 8);
    for d in &drafts[..6] {
        assert!(
            !d.locator.contains_key("heading")
                && !d.locator.contains_key("level")
                && !d.locator.contains_key("page"),
            "falsy values leaked: {:?}",
            d.locator
        );
        assert!(!d.metadata.contains_key("section_heading"));
    }
    assert_eq!(
        drafts[6].locator["heading"],
        Value::String("Kept".to_owned())
    );
    assert_eq!(drafts[6].locator["level"], Value::from(2));
    assert_eq!(drafts[6].locator["page"], Value::from(7));
    assert_eq!(
        drafts[6].metadata["section_heading"],
        Value::String("Kept".to_owned())
    );
    assert_eq!(drafts[7].locator["page"], Value::String("xiv".to_owned()));
    assert_offsets_are_true(text, &drafts);
}

#[test]
fn structural_reads_offsets_without_a_document_to_check() {
    // Explicit offsets with no full_text: trusted, unvalidated — the Python
    // slice is never even taken.
    let sections = vec![SectionInput {
        text: Some("Free-floating.".to_owned()),
        char_start: Some(40),
        char_end: Some(54),
        ..Default::default()
    }];
    let drafts = StructuralChunker::default()
        .chunk(&sections, None, None)
        .unwrap();
    assert_eq!(drafts.len(), 1);
    assert_eq!((drafts[0].char_start, drafts[0].char_end), (40, 54));
}

#[test]
fn structural_rejects_bad_spans() {
    // Outside the document, with a document to check against.
    let sections = vec![SectionInput {
        text: Some("Short.".to_owned()),
        char_start: Some(0),
        char_end: Some(400),
        ..Default::default()
    }];
    let err = StructuralChunker::default()
        .chunk(&sections, None, Some("Short."))
        .unwrap_err();
    assert!(err.to_string().contains("outside"), "unexpected: {err}");

    // Negative without a document: nothing to clamp against, refused.
    let sections = vec![SectionInput {
        text: Some("Short.".to_owned()),
        char_start: Some(-4),
        char_end: Some(2),
        ..Default::default()
    }];
    let err = StructuralChunker::default()
        .chunk(&sections, None, None)
        .unwrap_err();
    assert!(err.to_string().contains("no document"), "unexpected: {err}");

    // Located text that appears nowhere.
    let sections = vec![SectionInput {
        text: Some("Absent without leave.".to_owned()),
        ..Default::default()
    }];
    let err = StructuralChunker::default()
        .chunk(&sections, None, Some("Present text here."))
        .unwrap_err();
    assert!(err.to_string().contains("not found"), "unexpected: {err}");
}

#[test]
fn structural_boundary_read_past_the_edge_is_skipped() {
    // Offsets the document cannot honour read back as "" and skip.
    let sections = vec![
        SectionInput {
            char_start: Some(0),
            char_end: Some(400),
            ..Default::default()
        },
        SectionInput {
            char_start: Some(0),
            char_end: None,
            ..Default::default()
        },
        SectionInput {
            text: Some("Kept.".to_owned()),
            char_start: Some(0),
            char_end: Some(5),
            ..Default::default()
        },
    ];
    let drafts = StructuralChunker::default()
        .chunk(&sections, None, Some("Kept."))
        .unwrap();
    assert_eq!(drafts.len(), 1);
    assert_eq!(drafts[0].text, "Kept.");
}

#[test]
fn structural_falls_back_to_start_when_past_the_cursor() {
    // Sections out of document order: the cursor misses, the whole-document
    // scan catches — `find(raw, cursor) or find(raw)`.
    let text = "First half here. Second half here.";
    let sections = vec![
        SectionInput {
            text: Some("Second half here.".to_owned()),
            ..Default::default()
        },
        SectionInput {
            text: Some("First half here. ".to_owned()),
            ..Default::default()
        },
    ];
    let drafts = StructuralChunker::default()
        .chunk(&sections, None, Some(text))
        .unwrap();
    assert_eq!(drafts.len(), 2);
    assert_eq!(drafts[0].text, "Second half here.");
    assert_eq!(drafts[1].text, "First half here.");
    assert_offsets_are_true(text, &drafts);
}

// --- windows edges ---

#[test]
fn window_slides_back_from_the_bound_end() {
    let chain = vec![doc_node(0, 1_000, 0, "Chapter")];
    let plan = choose_window(
        Some(Span {
            start: 900,
            end: 950,
        }),
        &chain,
        600,
        50,
    )
    .unwrap();
    assert_eq!((plan.span.start, plan.span.end), (400, 1_000));
    assert_eq!(plan.source, WindowSource::NodeWindow);
}

#[test]
fn build_window_without_a_passage_span() {
    use marginalia_chunk::windows::WindowPlan;
    let plan = WindowPlan {
        span: Span { start: 10, end: 32 },
        source: WindowSource::DocumentWindow,
        node_id: None,
    };
    let raw = "   padded text here   ";
    let window = build_window(None, &plan, &[], Some(raw)).unwrap();
    assert_eq!((window.char_start, window.char_end), (13, 29));
    assert_eq!(window.text, "padded text here");
    assert!(window.breadcrumb.is_empty());

    // All whitespace with no passage floor to re-apply: nothing to hand back.
    assert_eq!(build_window(None, &plan, &[], Some("   ")), None);
}

// --- langconfig: the whole table ---

#[test]
fn langconfig_covers_every_shipped_stemmer() {
    let cases = [
        ("ar", "arabic"),
        ("da", "danish"),
        ("de", "german"),
        ("el", "greek"),
        ("en", "english"),
        ("es", "spanish"),
        ("eu", "basque"),
        ("fi", "finnish"),
        ("fr", "french"),
        ("ga", "irish"),
        ("hi", "hindi"),
        ("hu", "hungarian"),
        ("hy", "armenian"),
        ("id", "indonesian"),
        ("it", "italian"),
        ("lt", "lithuanian"),
        ("ne", "nepali"),
        ("nl", "dutch"),
        ("no", "norwegian"),
        ("pt", "portuguese"),
        ("ro", "romanian"),
        ("ru", "russian"),
        ("sr", "serbian"),
        ("sv", "swedish"),
        ("ta", "tamil"),
        ("tr", "turkish"),
        ("yi", "yiddish"),
    ];
    for (iso, config) in cases {
        assert_eq!(pg_config(Some(iso)), config, "iso {iso}");
        assert!(is_known_config(config), "config {config}");
    }
}

#[test]
fn sentence_spans_of_empty_is_empty() {
    assert!(sentence_spans("").is_empty());
    assert!(paragraph_spans("").is_empty());
}

#[test]
fn zero_token_budget_is_rejected_not_hung() {
    // `cap_spans` refuses max_tokens < 1; the error surfaces through both
    // chunkers instead of stalling the walk.
    let err = ProseWindowChunker::new(0, 0)
        .chunk("Some words here.", None)
        .unwrap_err();
    assert!(err.to_string().contains("max_tokens"), "unexpected: {err}");
    let sections = vec![SectionInput {
        text: Some("Short.".to_owned()),
        ..Default::default()
    }];
    let err = StructuralChunker::new(0, 0)
        .chunk(&sections, None, Some("Short."))
        .unwrap_err();
    assert!(err.to_string().contains("max_tokens"), "unexpected: {err}");
}
