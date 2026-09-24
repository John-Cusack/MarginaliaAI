//! Markdown acceptance: parity with `test_markdown_module.py`, the one
//! parser the accelerator ships.

use marginalia_parse::markdown;
use serde_json::json;

// --- markdown ---

const BOOK: &str = "# The Whole Thing\n\nOpening prose before any section.\n\n## Part One\n\nBody of part one, which runs for a while.\n\n### A Subsection\n\nDetail under part one, with **bold** and a [link](https://example.com).\n\n## Part Two\n\nBody of part two.";

#[test]
fn markdown_headings_survive_into_the_canonical_text() {
    let doc = markdown::parse_text(BOOK, "book.md");
    assert!(doc.text.contains("# The Whole Thing"));
    assert!(doc.text.contains("### A Subsection"));
    assert_eq!(doc.title.as_deref(), Some("The Whole Thing"));
}

#[test]
fn markdown_formatting_that_is_only_formatting_is_stripped() {
    let doc = markdown::parse_text(BOOK, "book.md");
    assert!(!doc.text.contains("**bold**") && doc.text.contains("bold"));
    assert!(!doc.text.contains("](https://example.com)") && doc.text.contains("a link"));
}

#[test]
fn markdown_document_without_headings_yields_no_sections() {
    let doc = markdown::parse_text("Just prose.\n\nMore prose.\n", "flat.md");
    assert!(doc.sections.is_empty());
    assert_eq!(doc.metadata["heading_count"], json!(0));
    assert_eq!(doc.title.as_deref(), Some("flat"));
    assert!(doc.text.starts_with("Just prose."));
}

#[test]
fn markdown_every_section_slices_back_to_its_own_heading() {
    let doc = markdown::parse_text(BOOK, "book.md");
    assert_eq!(doc.sections.len(), 4);
    for section in &doc.sections {
        let span = &doc.text[section["char_start"].as_u64().unwrap() as usize
            ..section["char_end"].as_u64().unwrap() as usize];
        assert!(span.starts_with('#'));
        assert!(span
            .split('\n')
            .next()
            .unwrap()
            .contains(section["heading"].as_str().unwrap()));
    }
}

#[test]
fn markdown_levels_come_from_the_marker_depth() {
    let doc = markdown::parse_text(BOOK, "book.md");
    let levels: Vec<u64> = doc
        .sections
        .iter()
        .map(|s| s["level"].as_u64().unwrap())
        .collect();
    assert_eq!(levels, [1, 2, 3, 2]);
}

#[test]
fn markdown_heading_inside_a_code_fence_is_not_a_section() {
    let doc = markdown::parse_text(
        "# Real Heading\n\nProse.\n\n```sh\n# not a heading\n```\n\nMore.\n",
        "fenced.md",
    );
    assert_eq!(doc.metadata["heading_count"], json!(1));
    assert_eq!(doc.sections[0]["heading"], json! {"Real Heading"});
}

#[test]
fn markdown_emphasis_torture() {
    assert_eq!(markdown::replace_emphasis("***a***"), "a");
    assert_eq!(markdown::replace_emphasis("**b**c"), "bc");
    assert_eq!(markdown::replace_emphasis("*unclosed"), "*unclosed");
    assert_eq!(markdown::replace_emphasis("a*b"), "a*b");
    assert_eq!(markdown::replace_emphasis("foo_bar_baz"), "foobarbaz");
    assert_eq!(markdown::replace_emphasis("*a\nb*"), "*a\nb*");
    assert_eq!(markdown::replace_emphasis("___x___"), "x");
    assert_eq!(markdown::replace_emphasis("****y****"), "*y*");
    assert_eq!(
        markdown::replace_emphasis("***bold _both_***"),
        "bold _both_"
    );
    assert_eq!(markdown::replace_emphasis(""), "");
}

#[test]
fn markdown_strip_order_images_before_links() {
    assert_eq!(
        markdown::strip_markdown("An ![alt text](img.png) and a [link](u) here."),
        "An alt text and a link here."
    );
}

#[test]
fn markdown_lists_quotes_rules_and_code() {
    let text = markdown::strip_markdown(
        "# T\n\n> quoted *em* text\n\n- alpha\n  1. one\n\n---\n\n`code` tail\n",
    );
    assert!(text.contains("# T"));
    assert!(text.contains("quoted em text"));
    assert!(text.contains("alpha") && !text.contains("- alpha"));
    assert!(text.contains("one") && !text.contains("1. one"));
    assert!(!text.contains("---"));
    assert!(text.contains("code") && !text.contains("`code`"));
}

#[test]
fn markdown_blank_runs_collapse_to_two() {
    assert_eq!(markdown::strip_markdown("a\n\n\n\nb"), "a\n\nb");
}

#[test]
fn markdown_titles() {
    assert_eq!(markdown::extract_title("#nospace\n", "f"), "f");
    assert_eq!(
        markdown::extract_title("##  Spaced Out  \n", "f"),
        "Spaced Out"
    );
    assert_eq!(markdown::extract_title("Title\n===\n", "s"), "s");
    assert_eq!(
        markdown::extract_title("# café — test\n", "f"),
        "café — test"
    );
}

#[test]
fn markdown_table_travels_in_sections_not_metadata() {
    let doc = markdown::parse_text(BOOK, "book.md");
    assert!(!doc.metadata.contains_key("sections"));
    assert_eq!(doc.metadata["format"], json!("markdown"));
}
#[test]
fn markdown_rules_take_trailing_blank_lines_greedily() {
    // Python's `^[-*_]{3,}\s*$` is greedy: `\s*` runs across the blank line
    // before `$` settles, so the rule and the whitespace line after it go
    // together. A lazy `\s*?` stopped at the first line end instead.
    assert_eq!(markdown::strip_markdown("a\n--- \n \nb"), "a\n\nb");
    // Values below are CPython 3.13's `_strip_markdown` outputs.
    assert_eq!(markdown::strip_markdown("a\n***\t\n\t\nb"), "a\nb");
    assert_eq!(markdown::strip_markdown("a\n___\nb"), "a\n_\nb");
}

#[test]
fn markdown_ordered_lists_use_python_digits() {
    // `\d` is Unicode 15.1 `Nd`, exactly CPython 3.13's: Arabic-Indic digits
    // mark a list item, U+1CCF0 OUTLINED DIGIT ZERO (Unicode 16) does not.
    assert_eq!(markdown::strip_markdown("\u{663}. item"), "item");
    assert_eq!(
        markdown::strip_markdown("\u{1CCF0}. item"),
        "\u{1CCF0}. item"
    );
}

#[test]
fn py_stem_follows_pathlib() {
    use marginalia_parse::py_stem;
    assert_eq!(py_stem("notes.txt"), "notes");
    assert_eq!(py_stem("archive.tar.gz"), "archive.tar");
    assert_eq!(py_stem(".bashrc"), ".bashrc");
    assert_eq!(py_stem("noext"), "noext");
    assert_eq!(py_stem("/a/b/c.md"), "c");
}
