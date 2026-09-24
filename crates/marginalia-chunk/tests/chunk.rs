//! Chunker acceptance: parity with `test_chunking.py`,
//! `test_chunker_contract.py`, and `test_structural_chunker.py` for the two
//! chunkers the accelerator ships (prose window and structural).

use marginalia_chunk::prose_window::{sentence_spans, ProseWindowChunker};
use marginalia_chunk::structural::{Offset, SectionInput, StructuralChunker};
use marginalia_types::sdk::PassageDraft;
use serde_json::{Map, Value};

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
            heading: Some(Value::from(format!("Heading {i}"))),
            level: Some(Value::from(1)),
            page: None,
            char_start: Some(Offset::from(start as i64)),
            char_end: Some(Offset::from(end as i64)),
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
            heading: Some(Value::from(format!("H{i}"))),
            level: Some(Value::from(1)),
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
            char_start: Some(Offset::from(0)),
            char_end: Some(Offset::from(english.chars().count() as i64)),
            level: Some(Value::from(1)),
            ..Default::default()
        },
        SectionInput {
            char_start: Some(Offset::from(english.chars().count() as i64)),
            char_end: Some(Offset::from(text.chars().count() as i64)),
            level: Some(Value::from(1)),
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

// --- identities: ids, versions, budgets ---

#[test]
fn chunker_identities_match_the_python_class_attrs() {
    use marginalia_chunk::{prose_window, structural};
    assert_eq!(prose_window::ID, "prose_window");
    assert_eq!(prose_window::CONSUMES, "text");
    assert_eq!(prose_window::VERSION, "4.0");
    assert_eq!(structural::ID, "structural");
    assert_eq!(structural::CONSUMES, "sections");
    assert_eq!(structural::VERSION, "4.0");

    assert_eq!(
        ProseWindowChunker::default().max_passage_tokens(),
        Some(500)
    );
    assert_eq!(StructuralChunker::default().max_passage_tokens(), Some(500));
}

// --- structural edges ---

#[test]
fn structural_skips_blank_sections_without_error() {
    let sections = vec![
        SectionInput {
            text: Some("   \n  ".to_owned()),
            heading: Some(Value::from("Blank".to_owned())),
            ..Default::default()
        },
        SectionInput {
            text: None,
            ..Default::default()
        },
        SectionInput {
            text: Some("Real body.".to_owned()),
            char_start: Some(Offset::from(0)),
            char_end: Some(Offset::from(10)),
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
            heading: Some(Value::from(String::new())),
            level: Some(Value::from(0)),
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
            heading: Some(Value::from("Kept".to_owned())),
            level: Some(Value::from(2)),
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
        char_start: Some(Offset::from(40)),
        char_end: Some(Offset::from(54)),
        ..Default::default()
    }];
    let drafts = StructuralChunker::default()
        .chunk(&sections, None, None)
        .unwrap();
    assert_eq!(drafts.len(), 1);
    assert_eq!((drafts[0].char_start, drafts[0].char_end), (40, 54));
}

#[test]
fn structural_spans_behave_like_python_slices() {
    // Every expectation below is CPython 3.13's `StructuralChunker` answer.
    // A span past the end clamps, as `full_text[0:400]` does.
    let sections = vec![SectionInput {
        text: Some("Short.".to_owned()),
        char_start: Some(Offset::from(0)),
        char_end: Some(Offset::from(400)),
        ..Default::default()
    }];
    let drafts = StructuralChunker::default()
        .chunk(&sections, None, Some("Short."))
        .unwrap();
    assert_eq!((drafts[0].char_start, drafts[0].char_end), (0, 6));

    // Without a document the reported start is the draft's start, unchecked;
    // the draft's own validator refuses a negative one (upstream raises the
    // same `PassageDraft` error while building it).
    let sections = vec![SectionInput {
        text: Some("Short.".to_owned()),
        char_start: Some(Offset::from(-4)),
        char_end: Some(Offset::from(2)),
        ..Default::default()
    }];
    let drafts = StructuralChunker::default()
        .chunk(&sections, None, None)
        .unwrap();
    assert_eq!((drafts[0].char_start, drafts[0].char_end), (-4, 2));
    let err = drafts[0].validate().unwrap_err().to_string();
    assert!(err.contains("non-negative"), "unexpected: {err}");

    // Located text that appears nowhere.
    let sections = vec![SectionInput {
        text: Some("Absent without leave.".to_owned()),
        ..Default::default()
    }];
    let err = StructuralChunker::default()
        .chunk(&sections, None, Some("Present text here."))
        .unwrap_err();
    assert_eq!(
        err.to_string(),
        "chunking a document failed: Section text not found in the document: 'Absent without leave.'"
    );
}

#[test]
fn structural_not_found_quotes_like_python_repr() {
    // The head renders with CPython `repr` quoting (single preferred), not
    // Rust `Debug`: pinned here because the seam differential caught `{:?}`
    // emitting doubles on plain section text.
    let sections = vec![SectionInput {
        text: Some("It's \"quoted\".".to_owned()),
        ..Default::default()
    }];
    let err = StructuralChunker::default()
        .chunk(&sections, None, Some("Present text here."))
        .unwrap_err();
    assert_eq!(
        err.to_string(),
        "chunking a document failed: Section text not found in the document: \
         'It\\'s \"quoted\".'"
    );
    let sections = vec![SectionInput {
        text: Some("line one\nline two".to_owned()),
        ..Default::default()
    }];
    let err = StructuralChunker::default()
        .chunk(&sections, None, Some("Present text here."))
        .unwrap_err();
    assert_eq!(
        err.to_string(),
        "chunking a document failed: Section text not found in the document: \
         'line one\\nline two'"
    );
}

#[test]
fn structural_boundary_read_past_the_edge_clamps() {
    // `full_text[0:400]` is the whole document, so a boundary-only section
    // running past the end is read back and kept, not dropped; a section
    // missing one offset reads back as "" and is skipped.
    let sections = vec![
        SectionInput {
            char_start: Some(Offset::from(0)),
            char_end: Some(Offset::from(400)),
            ..Default::default()
        },
        SectionInput {
            char_start: Some(Offset::from(0)),
            char_end: None,
            ..Default::default()
        },
        SectionInput {
            text: Some("Kept.".to_owned()),
            char_start: Some(Offset::from(0)),
            char_end: Some(Offset::from(5)),
            ..Default::default()
        },
    ];
    let drafts = StructuralChunker::default()
        .chunk(&sections, None, Some("Kept."))
        .unwrap();
    let got: Vec<(i64, i64, i64, &str)> = drafts
        .iter()
        .map(|d| (d.position, d.char_start, d.char_end, d.text.as_str()))
        .collect();
    assert_eq!(got, [(0, 0, 5, "Kept."), (1, 0, 5, "Kept.")]);
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
