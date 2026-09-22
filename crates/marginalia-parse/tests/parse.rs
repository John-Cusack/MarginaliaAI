//! Phase 3 acceptance: parity with `test_markdown_module.py`,
//! `test_html_and_tei_sections.py`, and `test_epub_module.py`, plus the
//! plain-text, PDF, detect, and corpus-script contracts the throwaway
//! differential proved byte-identical against CPython 3.13 (100 fixtures,
//! since removed).

use marginalia_parse::epub;
use marginalia_parse::html;
use marginalia_parse::markdown;
use marginalia_parse::pdf;
use marginalia_parse::plain_text;
use marginalia_parse::tei;
use serde_json::json;

fn parse_html(text: &str, name: &str) -> marginalia_types::sdk::ParsedDocument {
    html::parse_text(text, name).unwrap()
}

// --- plain_text ---

#[test]
fn plain_title_is_the_first_short_line() {
    let doc = plain_text::parse_text("Shopping List\n\nmilk\neggs\n", "notes.txt");
    assert_eq!(doc.title.as_deref(), Some("Shopping List"));
    assert_eq!(doc.text, "Shopping List\n\nmilk\neggs\n");
}

#[test]
fn plain_long_first_lines_fall_through() {
    let doc = plain_text::parse_text(&format!("{}\nshort title\n", "y".repeat(201)), "b.txt");
    assert_eq!(doc.title.as_deref(), Some("short title"));
}

#[test]
fn plain_two_hundred_chars_still_titles() {
    let doc = plain_text::parse_text(&format!("{}\nbody\n", "z".repeat(200)), "e.txt");
    assert_eq!(doc.title.as_deref(), Some("z".repeat(200).as_str()));
}

#[test]
fn plain_no_usable_line_falls_back_to_the_stem() {
    let doc = plain_text::parse_text("", "empty.txt");
    assert_eq!(doc.title.as_deref(), Some("empty"));
    assert_eq!(doc.text, "");
}

#[test]
fn plain_counts_chars_and_splitlines_lines() {
    let doc = plain_text::parse_bytes("a\r\nb\rc\n".as_bytes(), "end.txt").unwrap();
    assert_eq!(doc.text, "a\nb\nc\n");
    assert_eq!(doc.metadata["char_count"], json!(6));
    assert_eq!(doc.metadata["line_count"], json!(3));
    assert_eq!(doc.metadata["file_name"], json!("end.txt"));
}

#[test]
fn plain_strict_bytes_fail() {
    assert!(plain_text::parse_bytes(b"caf\xe9\n", "latin.dat").is_err());
}

#[test]
fn plain_detect_scores() {
    assert_eq!(
        plain_text::detect("notes.txt", Some(b"head".as_slice())),
        (0.8, "extension '.txt' matches plain text".to_owned())
    );
    assert_eq!(
        plain_text::detect("notes.dat", Some(b"head".as_slice())),
        (
            0.3,
            "file is valid UTF-8 text (fallback detection)".to_owned()
        )
    );
    assert_eq!(
        plain_text::detect("notes.dat", None),
        (0.0, "not detected as plain text".to_owned())
    );
    // Undecodable bytes fail the fallback the way the strict head read fails.
    assert_eq!(
        plain_text::detect("notes.dat", Some(b"caf\xe9".as_slice())),
        (0.0, "not detected as plain text".to_owned())
    );
    assert_eq!(plain_text::MODULE_VERSION, "1.0");
    assert_eq!(plain_text::DEFAULT_CHUNKER, "prose_window");
    assert_eq!(plain_text::DEFAULT_DOCUMENT_TYPE, "generic");
}

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
fn markdown_crlf_reads_as_lf() {
    let doc = markdown::parse_bytes("# Head\r\n\r\nBody **bold**.\r\n".as_bytes(), "c.md").unwrap();
    assert_eq!(doc.text, "# Head\n\nBody bold.");
}

#[test]
fn markdown_invalid_bytes_fail() {
    assert!(markdown::parse_bytes(b"# T\n\xff\n", "bad.md").is_err());
}

#[test]
fn markdown_detect_scores() {
    assert_eq!(
        markdown::detect("book.md", None),
        (0.9, "extension '.md' matches markdown".to_owned())
    );
    assert_eq!(
        markdown::detect("book.txt", Some(b"# Head\n".as_slice())),
        (0.4, "file contains markdown headings".to_owned())
    );
    assert_eq!(
        markdown::detect("book.txt", Some(b"prose\n".as_slice())),
        (0.0, "not detected as markdown".to_owned())
    );
    assert_eq!(
        markdown::detect("book.txt", None),
        (0.0, "not detected as markdown".to_owned())
    );
    // Undecodable bytes skip the peek the way the strict head read skips it.
    assert_eq!(
        markdown::detect("book.txt", Some(b"# T\n\xff\n".as_slice())),
        (0.0, "not detected as markdown".to_owned())
    );
}

#[test]
fn markdown_table_travels_in_sections_not_metadata() {
    let doc = markdown::parse_text(BOOK, "book.md");
    assert!(!doc.metadata.contains_key("sections"));
    assert_eq!(doc.metadata["format"], json!("markdown"));
    assert_eq!(markdown::DEFAULT_CHUNKER, "structural");
}
// --- html ---

const HTML: &str = "<!doctype html><html lang=\"en\"><head><title>A Page</title></head><body>\n<!-- a comment, which get_text() excludes -->\n<h1>Top Heading</h1><p>Intro paragraph.</p>\n<h2>First <em>Nested</em> Section</h2><p>Body of first.</p>\n<script>var x = 1;</script>\n<h2>Second Section</h2><ul><li>alpha</li><li>beta</li></ul>\n</body></html>";

#[test]
fn html_canonical_text_is_unchanged() {
    let doc = parse_html(HTML, "page.html");
    assert_eq!(
        doc.text,
        "Top Heading\nIntro paragraph.\nFirst\nNested\nSection\nBody of first.\nSecond Section\nalpha\nbeta"
    );
    assert!(!doc.text.contains("a comment"));
    assert!(!doc.text.contains("var x = 1;"));
}

#[test]
fn html_every_section_starts_at_its_own_heading() {
    let doc = parse_html(HTML, "page.html");
    assert_eq!(doc.sections.len(), 3);
    for section in &doc.sections {
        let span = &doc.text[section["char_start"].as_u64().unwrap() as usize
            ..section["char_end"].as_u64().unwrap() as usize];
        let flat = span.split_whitespace().collect::<Vec<_>>().join(" ");
        assert!(flat.starts_with(section["heading"].as_str().unwrap()));
    }
}

#[test]
fn html_label_normalises_whitespace_the_text_keeps() {
    let doc = parse_html(HTML, "page.html");
    assert_eq!(doc.sections[1]["heading"], json! {"First Nested Section"});
    let span = &doc.text[doc.sections[1]["char_start"].as_u64().unwrap() as usize
        ..doc.sections[1]["char_end"].as_u64().unwrap() as usize];
    assert!(span.starts_with("First\nNested\nSection"));
}

#[test]
fn html_levels_come_from_the_tag_number() {
    let doc = parse_html(HTML, "page.html");
    let levels: Vec<u64> = doc
        .sections
        .iter()
        .map(|s| s["level"].as_u64().unwrap())
        .collect();
    assert_eq!(levels, [1, 2, 2]);
}

#[test]
fn html_page_without_headings_yields_no_sections() {
    let doc = parse_html("<html><body><p>Just prose.</p></body></html>", "flat.html");
    assert!(doc.sections.is_empty());
    assert_eq!(doc.text, "Just prose.");
}

#[test]
fn html_titles() {
    assert_eq!(
        parse_html("<title>Solo</title><p>x</p>", "f.html")
            .title
            .as_deref(),
        Some("Solo")
    );
    assert_eq!(
        parse_html("<title>A <b>Page</b></title><h1>H</h1><p>x</p>", "f.html")
            .title
            .as_deref(),
        Some("A <b>Page</b>")
    );
    assert_eq!(
        parse_html("<title><!--hidden--></title><p>x</p>", "f.html")
            .title
            .as_deref(),
        Some("<!--hidden-->")
    );
    assert_eq!(
        parse_html("<title>  </title><p>x</p>", "f.html")
            .title
            .as_deref(),
        Some("")
    );
    assert_eq!(
        parse_html("<h1>Main <i>Thing</i></h1><p>x</p>", "f.html")
            .title
            .as_deref(),
        Some("MainThing")
    );
    assert_eq!(
        parse_html("<p>x</p>", "stem.html").title.as_deref(),
        Some("stem")
    );
}

#[test]
fn html_fragment_without_body_reads_whole() {
    let doc = parse_html("<title>Frag Title</title><p>Hi there.</p>", "frag.html");
    assert_eq!(doc.text, "Frag Title\nHi there.");
    assert_eq!(doc.title.as_deref(), Some("Frag Title"));
}

#[test]
fn html_explicit_body_matrix() {
    assert!(html::has_explicit_body("<body><p>x</p></body>"));
    assert!(html::has_explicit_body("<BODY>"));
    assert!(html::has_explicit_body("<body class=\"x\">"));
    assert!(html::has_explicit_body("<body/>"));
    assert!(!html::has_explicit_body("<p>x</p>"));
    assert!(!html::has_explicit_body("<!--<body>--><p>x</p>"));
    assert!(!html::has_explicit_body(
        "<script>var s = '<body>';</script><p>x</p>"
    ));
    assert!(!html::has_explicit_body("<title><body></title><p>x</p>"));
    assert!(html::has_explicit_body("<noscript><body></noscript>"));
}

#[test]
fn html_metadata_and_counts() {
    let doc = parse_html(
        "<html lang=\"fr\"><head><title>T</title><meta name=\"description\" content=\"D\">\
         <meta name=\"author\" content=\"First\"><meta name=\"author\" content=\"Second\">\
         <meta property=\"description\" content=\"P\"></head>\
         <body><h1>H</h1><p>x</p><a href=\"u\">l</a><a>no href</a></body></html>",
        "meta.html",
    );
    assert_eq!(doc.metadata["description"], json!("P"));
    assert_eq!(doc.metadata["author"], json!("Second"));
    assert_eq!(doc.metadata["language"], json!("fr"));
    assert_eq!(doc.language.as_deref(), Some("fr"));
    assert_eq!(doc.metadata["heading_count"], json!(1));
    assert_eq!(doc.metadata["link_count"], json!(1));
}

#[test]
fn html_entities_matrix() {
    let doc = parse_html("<p>a&amp;b&#65;&#x42;&bogus;&amp</p>", "e.html");
    assert_eq!(doc.text, "a&bAB&bogus&");
    let doc = parse_html(
        "<p>A&amp B&amp;C &lt= &AMP; &#38 &#x26 &amp<b>&lt3</p>",
        "e.html",
    );
    assert_eq!(doc.text, "A& B&C <= & & & &\n&lt3");
    let doc = parse_html("<p>&copy; &nbsp; &unknown; tail</p>", "e.html");
    assert_eq!(doc.text, "© \u{a0} &unknown tail");
}

#[test]
fn html_malformed_references_fail() {
    assert!(html::parse_bytes("<p>A&#38b</p>".as_bytes(), "c.html").is_err());
}

#[test]
fn html_detect_scores() {
    assert_eq!(
        html::detect("page.html", None),
        (0.9, "extension '.html' matches HTML".to_owned())
    );
    assert_eq!(
        html::detect("page.htm", None),
        (0.9, "extension '.htm' matches HTML".to_owned())
    );
    assert_eq!(
        html::detect("page.txt", Some("<html><p>x</p>")),
        (0.7, "file contains HTML markers".to_owned())
    );
    assert_eq!(
        html::detect("page.txt", Some("<!DOCTYPE html><p>x</p>")),
        (0.7, "file contains HTML markers".to_owned())
    );
    assert_eq!(
        html::detect("page.txt", Some("plain")),
        (0.0, "not detected as HTML".to_owned())
    );
    assert_eq!(html::DEFAULT_CHUNKER, "structural");
}

#[test]
fn html_empty_and_bare() {
    let doc = parse_html("", "empty.html");
    assert_eq!(doc.text, "");
    assert_eq!(doc.title.as_deref(), Some("empty"));
    let doc = parse_html("just text, no tags", "bare.html");
    assert_eq!(doc.text, "just text, no tags");
}
// --- tei ---

const TEI: &str = "<?xml version=\"1.0\"?>\n<TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><teiHeader><fileDesc><titleStmt>\n<title>A Treatise</title><author>Some Author</author></titleStmt></fileDesc></teiHeader>\n<text><body>\n<div><head>Part One</head><p>Intro to part one.</p>\n  <div><head>Chapter A</head><p>Alpha body.</p></div>\n  <div><head>Chapter B</head><p>Beta body.</p></div>\n</div>\n</body></text></TEI>";

fn parse_tei(bytes: &[u8], name: &str) -> marginalia_types::sdk::ParsedDocument {
    tei::parse_bytes(bytes, name).unwrap()
}

#[test]
fn tei_nested_chapter_is_stored_once() {
    let doc = parse_tei(TEI.as_bytes(), "doc.xml");
    assert_eq!(doc.text.matches("Alpha body.").count(), 1);
    assert_eq!(doc.text.matches("Beta body.").count(), 1);
}

#[test]
fn tei_blocks_are_separated_rather_than_welded() {
    let doc = parse_tei(TEI.as_bytes(), "doc.xml");
    assert!(!doc.text.contains("AAlpha"));
    assert!(!doc.text.contains("OneIntro"));
    assert!(doc.text.contains("Chapter A\nAlpha body."));
}

#[test]
fn tei_nesting_depth_becomes_the_level() {
    let doc = parse_tei(TEI.as_bytes(), "doc.xml");
    let pairs: Vec<(String, u64)> = doc
        .sections
        .iter()
        .map(|s| {
            (
                s["heading"].as_str().unwrap().to_owned(),
                s["level"].as_u64().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        pairs,
        [
            ("Part One".to_owned(), 1),
            ("Chapter A".to_owned(), 2),
            ("Chapter B".to_owned(), 2)
        ]
    );
}

#[test]
fn tei_parent_section_stops_at_its_first_child() {
    let doc = parse_tei(TEI.as_bytes(), "doc.xml");
    let (start0, end0) = (
        doc.sections[0]["char_start"].as_u64().unwrap(),
        doc.sections[0]["char_end"].as_u64().unwrap(),
    );
    let start1 = doc.sections[1]["char_start"].as_u64().unwrap();
    assert!(end0 <= start1);
    assert_eq!(
        &doc.text[start0 as usize..end0 as usize],
        "Part One\nIntro to part one."
    );
}

#[test]
fn tei_body_without_divs_still_produces_text() {
    let doc = parse_tei(
        "<?xml version=\"1.0\"?><TEI xmlns=\"http://www.tei-c.org/ns/1.0\">\
         <teiHeader/><text><body><p>Only prose here.</p></body></text></TEI>"
            .as_bytes(),
        "flat.xml",
    );
    assert_eq!(doc.text, "Only prose here.");
    assert!(doc.sections.is_empty());
}

#[test]
fn tei_header_without_body_yields_empty_text() {
    let doc = parse_tei(
        "<TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><teiHeader><fileDesc><titleStmt>\
         <title>Only Header</title></titleStmt></fileDesc></teiHeader></TEI>"
            .as_bytes(),
        "nb.xml",
    );
    assert_eq!(doc.text, "");
    assert_eq!(doc.title.as_deref(), Some("Only Header"));
}

#[test]
fn tei_non_namespaced_documents_parse() {
    let doc = parse_tei(
        "<TEI><teiHeader><fileDesc><titleStmt><title>Bare</title></titleStmt>\
         </fileDesc></teiHeader><text><body><div><head>H</head><p>B.</p></div>\
         </body></text></TEI>"
            .as_bytes(),
        "bare.xml",
    );
    assert_eq!(doc.title.as_deref(), Some("Bare"));
    assert_eq!(doc.text, "H\nB.");
}

#[test]
fn tei_empty_head_keeps_an_empty_heading() {
    let doc = parse_tei(
        "<TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><text><body><div><head> </head>\
         <p>x</p></div></body></text></TEI>"
            .as_bytes(),
        "eh.xml",
    );
    assert_eq!(doc.sections.len(), 1);
    assert_eq!(doc.sections[0]["heading"], json!(""));
}

#[test]
fn tei_dates_prefer_the_when_attribute() {
    let doc = parse_tei(
        "<TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><teiHeader><fileDesc><titleStmt>\
         <title>D</title></titleStmt><publicationStmt><date when=\"1500\">long ago</date>\
         </publicationStmt></fileDesc></teiHeader><text><body><p>x</p></body></text></TEI>"
            .as_bytes(),
        "d.xml",
    );
    assert_eq!(doc.metadata["date"], json!("1500"));
}

#[test]
fn tei_counts_and_format() {
    let doc = parse_tei(
        "<TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><text><body><div><head>H</head>\
         <p>x<note>n1</note><note>n2</note><bibl>b1</bibl></p></div></body></text></TEI>"
            .as_bytes(),
        "c.xml",
    );
    assert_eq!(doc.metadata["note_count"], json!(2));
    assert_eq!(doc.metadata["bibliography_count"], json!(1));
    assert_eq!(doc.metadata["div_count"], json!(1));
    assert_eq!(doc.metadata["format"], json!("tei_xml"));
    assert_eq!(tei::DEFAULT_DOCUMENT_TYPE, "scholarly");
}

#[test]
fn tei_latin1_declaration_decodes() {
    let doc = parse_tei(
        b"<?xml version=\"1.0\" encoding=\"ISO-8859-1\"?><TEI><text><body><p>caf\xe9</p></body></text></TEI>",
        "latin.xml",
    );
    assert_eq!(doc.text, "caf\u{e9}");
}

#[test]
fn tei_failures_are_failures() {
    // Comment directly under a div: reading text off it fails upstream too.
    assert!(tei::parse_bytes(
        "<TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><text><body><div><head>H</head>\
         <!--gone--><p>a</p></div></body></text></TEI>"
            .as_bytes(),
        "cm.xml"
    )
    .is_err());
    assert!(tei::parse_bytes(b"<TEI><text><p>&bogus;</p></body></text></TEI>", "be.xml").is_err());
    assert!(tei::parse_bytes(b"<TEI><text><p>x</q></p></body></text></TEI>", "mm.xml").is_err());
    assert!(tei::parse_bytes(b"", "empty.xml").is_err());
    assert!(tei::parse_bytes(b"<TEI><text><body><p>x</p></body>", "un.xml").is_err());
    // External subsets are never fetched: silently ignored, like upstream.
    assert!(
        tei::parse_bytes(
            b"<!DOCTYPE TEI SYSTEM \"http://example.invalid/tei.dtd\">\
              <TEI><text><body><p>x</p></body></text></TEI>",
            "ex.xml"
        )
        .unwrap()
        .text
            == "x"
    );
}

#[test]
fn tei_detect_scores() {
    assert_eq!(
        tei::detect(
            "doc.xml",
            Some("<TEI xmlns=\"http://www.tei-c.org/ns/1.0\">")
        ),
        (0.95, "file contains TEI namespace declaration".to_owned())
    );
    assert_eq!(
        tei::detect("doc.xml", Some("<TEI>")),
        (0.7, "file contains <TEI> root element".to_owned())
    );
    assert_eq!(
        tei::detect("doc.xml", Some("<p>x</p>")),
        (0.0, "not detected as TEI XML".to_owned())
    );
    assert_eq!(
        tei::detect("doc.txt", Some("<TEI>")),
        (0.0, "extension '.txt' does not match XML".to_owned())
    );
}

// --- epub ---

/// Build a minimal EPUB in memory: the manifest order follows `order` but
/// the spine follows slice order, so reading-order bugs show.
#[allow(clippy::too_many_arguments)]
fn build_epub(
    title: &str,
    chapters: &[(&str, &str, &str)],
    manifest_order: &[usize],
    ncx: bool,
    nav: bool,
    spine_toc: bool,
    extra_head: &str,
) -> Vec<u8> {
    use std::io::Write;
    let mut buf = Vec::new();
    {
        let mut zip = zip::ZipWriter::new(std::io::Cursor::new(&mut buf));
        let opts = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Stored);
        let mut add = |name: &str, content: &str| {
            zip.start_file(name, opts).unwrap();
            zip.write_all(content.as_bytes()).unwrap();
        };
        add(
            "META-INF/container.xml",
            "<?xml version=\"1.0\"?><container xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">\
             <rootfiles><rootfile media-type=\"application/oebps-package+xml\" \
             full-path=\"EPUB/content.opf\"/></rootfiles></container>",
        );
        let mut manifest = String::new();
        for (id, (file, _, _)) in chapters.iter().enumerate() {
            manifest.push_str(&format!(
                "<item href=\"{file}\" id=\"ch{id}\" media-type=\"application/xhtml+xml\"/>"
            ));
        }
        // Manifest order deliberately differs from spine order.
        let mut manifest_items = String::new();
        for index in manifest_order {
            let (file, _, _) = chapters[*index];
            manifest_items.push_str(&format!(
                "<item href=\"{file}\" id=\"ch{index}\" media-type=\"application/xhtml+xml\"/>"
            ));
        }
        let _ = manifest;
        if ncx {
            manifest_items.push_str(
                "<item href=\"toc.ncx\" id=\"ncx\" media-type=\"application/x-dtbncx+xml\"/>",
            );
        }
        if nav {
            manifest_items.push_str(
                "<item href=\"nav.xhtml\" id=\"nav\" media-type=\"application/xhtml+xml\" properties=\"nav\"/>",
            );
        }
        let spine_refs: String = chapters
            .iter()
            .enumerate()
            .map(|(id, _)| format!("<itemref idref=\"ch{id}\"/>"))
            .collect();
        let toc_attr = if spine_toc && ncx { " toc=\"ncx\"" } else { "" };
        add(
            "EPUB/content.opf",
            &format!(
                "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
                 <metadata><dc:title xmlns:dc=\"http://purl.org/dc/elements/1.1/\">{title}</dc:title>\
                 {extra_head}</metadata><manifest>{manifest_items}</manifest>\
                 <spine{toc_attr}>{spine_refs}</spine></package>"
            ),
        );
        for (file, heading, body) in chapters {
            add(
                &format!("EPUB/{file}"),
                &format!("<html><body><h1>{heading}</h1><p>{body}</p></body></html>"),
            );
        }
        if ncx {
            let points: String = chapters
                .iter()
                .map(|(file, heading, _)| {
                    format!(
                        "<navPoint><navLabel><text>{heading}</text></navLabel>\
                         <content src=\"{file}\"/></navPoint>"
                    )
                })
                .collect();
            add(
                "EPUB/toc.ncx",
                &format!(
                    "<?xml version=\"1.0\"?><ncx xmlns=\"http://www.daisy.org/z3986/2005/ncx/\">\
                     <navMap>{points}</navMap></ncx>"
                ),
            );
        }
        if nav {
            let items: String = chapters
                .iter()
                .map(|(file, heading, _)| format!("<li><a href=\"{file}\">{heading}</a></li>"))
                .collect();
            add(
                "EPUB/nav.xhtml",
                &format!("<html><body><nav epub:type=\"toc\"><ol>{items}</ol></nav></body></html>"),
            );
        }
        zip.finish().unwrap();
    }
    buf
}

fn three_chapters() -> Vec<(&'static str, &'static str, &'static str)> {
    vec![
        ("c1.xhtml", "Chapter One", "AAA first chapter body."),
        ("c2.xhtml", "Chapter Two", "BBB second chapter body."),
        ("c3.xhtml", "Chapter Three", "CCC third chapter body."),
    ]
}

#[test]
fn epub_canonical_text_follows_the_spine() {
    let bytes = build_epub(
        "Order Test",
        &three_chapters(),
        &[2, 0, 1],
        true,
        true,
        true,
        "",
    );
    let doc = epub::parse_bytes(&bytes, "order.epub").unwrap();
    assert!(doc.text.find("AAA").unwrap() < doc.text.find("BBB").unwrap());
    assert!(doc.text.find("BBB").unwrap() < doc.text.find("CCC").unwrap());
}

#[test]
fn epub_navigation_document_is_not_ingested_as_prose() {
    let bytes = build_epub(
        "Order Test",
        &three_chapters(),
        &[2, 0, 1],
        true,
        true,
        true,
        "",
    );
    let doc = epub::parse_bytes(&bytes, "order.epub").unwrap();
    assert_eq!(doc.metadata["chapter_count"], json!(3));
    assert_eq!(doc.text.matches("Chapter Three").count(), 1);
}

#[test]
fn epub_sections_address_the_canonical_text() {
    let bytes = build_epub(
        "Order Test",
        &three_chapters(),
        &[2, 0, 1],
        true,
        true,
        true,
        "",
    );
    let doc = epub::parse_bytes(&bytes, "order.epub").unwrap();
    assert_eq!(doc.sections.len(), 3);
    for section in &doc.sections {
        let span = &doc.text[section["char_start"].as_u64().unwrap() as usize
            ..section["char_end"].as_u64().unwrap() as usize];
        assert!(span.starts_with(section["heading"].as_str().unwrap()));
        // Boundaries only: the prose is stored once.
        assert!(!section.contains_key("text"));
    }
    // The table travels in `sections`, and the chunker stays structural.
    assert_eq!(epub::DEFAULT_CHUNKER, "structural");
    assert_eq!(epub::DEFAULT_DOCUMENT_TYPE, "book");
}

#[test]
fn epub_nav_table_fills_a_missing_ncx() {
    let bytes = build_epub(
        "Nav Only",
        &three_chapters(),
        &[0, 1, 2],
        false,
        true,
        false,
        "",
    );
    let doc = epub::parse_bytes(&bytes, "nav.epub").unwrap();
    assert_eq!(doc.sections.len(), 3);
    assert_eq!(doc.sections[0]["heading"], json!("Chapter One"));
    assert_eq!(doc.sections[0]["level"], json!(1));
}

#[test]
fn epub_blank_chapters_are_skipped() {
    let chapters = vec![
        ("c1.xhtml", "Chapter One", "AAA"),
        ("blank.xhtml", "", "   "),
        ("c2.xhtml", "Chapter Two", "BBB"),
    ];
    let bytes = build_epub("T", &chapters, &[0, 1, 2], true, false, true, "");
    let doc = epub::parse_bytes(&bytes, "t.epub").unwrap();
    assert_eq!(doc.metadata["chapter_count"], json!(2));
}

#[test]
fn epub_broken_ncx_reference_fails() {
    // The spine names an NCX id the manifest never carries.
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine toc=\"gon\"><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    let bytes = raw_epub(CONTAINER, opf, &[("EPUB/c1.xhtml", ch.as_slice())]);
    match epub::parse_bytes(&bytes, "t.epub") {
        Err(marginalia_types::Error::Parse(msg)) => assert_eq!(msg, "cannot find NCX file"),
        other => panic!("wrong result: {other:?}"),
    }
}

#[test]
fn epub_not_a_zip_fails() {
    assert!(epub::parse_bytes(b"not a zip", "t.epub").is_err());
}

#[test]
fn epub_detect_scores() {
    assert_eq!(
        epub::detect("book.epub", &[]),
        (0.9, "extension '.epub' matches EPUB".to_owned())
    );
    assert_eq!(
        epub::detect("book.zip", b"PK\x03\x04rest"),
        (0.2, "file is a ZIP archive (could be EPUB)".to_owned())
    );
    assert_eq!(
        epub::detect("book.zip", b"data"),
        (0.0, "not detected as EPUB".to_owned())
    );
}

#[test]
fn epub_posix_paths_and_unquoting() {
    assert_eq!(epub::posix_normpath("OEBPS/../c1.xhtml"), "c1.xhtml");
    assert_eq!(epub::posix_normpath("a/./b"), "a/b");
    assert_eq!(epub::posix_normpath("//a"), "//a");
    assert_eq!(epub::posix_normpath(""), ".");
    assert_eq!(epub::posix_normpath("/../a"), "/a");
    assert_eq!(epub::unquote("ch%201.xhtml"), "ch 1.xhtml");
    assert_eq!(epub::unquote("a+b"), "a+b");
    assert_eq!(epub::unquote("100%"), "100%");
    assert_eq!(epub::unquote("%ZZ"), "%ZZ");
}
// --- pdf ---

fn fixture_pdf() -> Vec<u8> {
    std::fs::read(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/basic.pdf"
    ))
    .unwrap()
}

#[test]
fn pdf_fixture_parses() {
    let doc = pdf::parse_bytes(&fixture_pdf(), "basic.pdf").unwrap();
    assert_eq!(doc.title.as_deref(), Some("Meta Title"));
    assert_eq!(doc.metadata["page_count"], json!(1));
    assert_eq!(doc.metadata["pdf_author"], json!("An Author"));
}

#[test]
fn pdf_assembly_rules() {
    use marginalia_parse::pdf::{assemble, PdfMeta};
    let meta = PdfMeta {
        title: "Meta Title".to_owned(),
        author: "A".to_owned(),
        ..Default::default()
    };
    // Blank pages drop; the rest join with a blank line.
    let doc = assemble(&["  ".to_owned(), "Body.".to_owned()], 2, &meta, "f.pdf");
    assert_eq!(doc.text, "Body.");
    assert_eq!(doc.title.as_deref(), Some("Meta Title"));
    assert_eq!(doc.metadata["page_count"], json!(2));
    assert!(!doc.metadata.contains_key("pdf_subject"));
    // No metadata title: the first short line wins, then the stem.
    let bare = PdfMeta::default();
    let doc = assemble(&["Guessed Title\n\nBody.".to_owned()], 1, &bare, "f.pdf");
    assert_eq!(doc.title.as_deref(), Some("Guessed Title"));
    let long = format!("{}\nReal\nBody.", "x".repeat(400));
    let doc = assemble(&[long], 1, &bare, "f.pdf");
    assert_eq!(doc.title.as_deref(), Some("Real"));
    let doc = assemble(&["   ".to_owned()], 1, &bare, "stem.pdf");
    assert_eq!(doc.text, "");
    assert_eq!(doc.title.as_deref(), Some("stem"));
    assert_eq!(pdf::DEFAULT_CHUNKER, "prose_window");
}

#[test]
fn pdf_garbage_fails() {
    assert!(pdf::parse_bytes(b"this is not a pdf", "g.pdf").is_err());
    assert!(pdf::extract_pages(b"this is not a pdf").is_err());
    assert!(pdf::page_count(b"this is not a pdf").is_err());
    assert!(pdf::info_string(b"this is not a pdf", "Title").is_err());
}

#[test]
fn pdf_strings_and_detect() {
    assert_eq!(pdf::pdf_string(&[0xFE, 0xFF, 0x00, 0x41]), "A");
    assert_eq!(pdf::pdf_string(b"plain"), "plain");
    assert_eq!(
        pdf::detect("doc.pdf", &[]),
        (0.9, "extension '.pdf' matches PDF".to_owned())
    );
    assert_eq!(
        pdf::detect("doc.bin", b"%PDF-1.7"),
        (0.9, "file starts with PDF magic bytes".to_owned())
    );
    assert_eq!(
        pdf::detect("doc.bin", b"data"),
        (0.0, "not detected as PDF".to_owned())
    );
}

// --- xml ---

use marginalia_parse::xml::{parse_xml, TextModel};

#[test]
fn xml_text_models_differ_on_comments() {
    let raw = "<r><div>t1<!--c-->t2<?p?>t3</div></r>";
    let lxml = parse_xml(raw.as_bytes(), TextModel::Lxml).unwrap();
    let div = lxml.root.find_child("div").unwrap();
    assert_eq!(div.text(), "t1");
    assert_eq!(div.itertext(), "t1t2t3");
    let et = parse_xml(raw.as_bytes(), TextModel::Et).unwrap();
    assert_eq!(et.root.find_child("div").unwrap().text(), "t1t2t3");
}

#[test]
fn xml_encodings_and_bom() {
    assert!(parse_xml(b"\xef\xbb\xbf<r/>", TextModel::Lxml).is_ok());
    assert_eq!(
        parse_xml(
            b"<?xml version=\"1.0\" encoding=\"ISO-8859-1\"?><r><p>caf\xe9</p></r>",
            TextModel::Lxml
        )
        .unwrap()
        .root
        .findall_descendants("p")[0]
            .text(),
        "caf\u{e9}"
    );
    assert!(parse_xml(b"<?xml encoding=\"bogus-9\"?><r/>", TextModel::Lxml).is_err());
    assert!(parse_xml(b"<r><p>caf\xe9</p></r>", TextModel::Lxml).is_err());
}

#[test]
fn xml_entities() {
    let doc = parse_xml(
        "<!DOCTYPE r [<!ENTITY q \"Q\">]><r><p>a&q;b &#65; &lt;</p></r>".as_bytes(),
        TextModel::Lxml,
    )
    .unwrap();
    assert_eq!(doc.root.find_child("p").unwrap().text(), "aQb A <");
    assert!(parse_xml(b"<r>&bogus;</r>", TextModel::Lxml).is_err());
    assert!(parse_xml(
        "<!DOCTYPE r [<!ENTITY q \"&q;\">]><r><p>&q;</p></r>".as_bytes(),
        TextModel::Lxml
    )
    .is_err());
    // External subsets are never fetched: silently ignored.
    assert!(parse_xml(
        b"<!DOCTYPE r SYSTEM \"http://example.invalid/x.dtd\"><r><p>t</p></r>",
        TextModel::Lxml
    )
    .is_ok());
}

#[test]
fn xml_namespaces() {
    let doc = parse_xml(
        "<r xmlns=\"urn:default\" xmlns:p=\"urn:p\"><p:a xmlns:p=\"urn:q\">x</p:a><b>y</b></r>"
            .as_bytes(),
        TextModel::Lxml,
    )
    .unwrap();
    assert_eq!(doc.root.tag(), "{urn:default}r");
    let b = doc.root.find_child("{urn:default}b").unwrap();
    assert_eq!(b.text(), "y");
    let renamed = doc.root.findall_descendants("{urn:q}a");
    assert_eq!(renamed.len(), 1);
    assert_eq!(renamed[0].text(), "x");
    assert!(parse_xml(b"<p:q xmlns=\"urn:x\"/>", TextModel::Lxml).is_err());
    assert!(parse_xml(b"<p:q/>", TextModel::Lxml).is_err());
}

#[test]
fn xml_malformed_inputs_fail() {
    for raw in [
        "<r><a></b></r>",
        "<r>",
        "",
        "<a/><b/>",
        "text<r/>",
        "<r/>tail",
        "  <?xml version=\"1.0\"?><r/>",
        "<!--c--><?xml version=\"1.0\"?><r/>",
        "<r></r></o>",
    ] {
        assert!(
            parse_xml(raw.as_bytes(), TextModel::Lxml).is_err(),
            "{raw:?}"
        );
    }
    // Comments and doctypes may trail the root; declarations may not.
    assert!(parse_xml(b"<r/><!--c-->", TextModel::Lxml).is_ok());
    assert!(parse_xml(b"<r/><?xml version=\"1.0\"?>", TextModel::Lxml).is_err());
}

#[test]
fn xml_paths_tails_and_segments() {
    let doc = parse_xml(
        "<r><h><t>t</t><a>x</a></h><h><t>t2</t></h></r>".as_bytes(),
        TextModel::Lxml,
    )
    .unwrap();
    assert!(doc.find("h", "").is_some());
    assert_eq!(doc.findall(".//t", "").len(), 2);
    assert_eq!(doc.find(".//h/t", "").unwrap().text(), "t");
    let h = doc.find("h", "").unwrap();
    assert_eq!(h.tail_after(99), "");
    assert_eq!(h.text_segments(), vec!["t", "x"]);
}
// --- pystr ---

use marginalia_parse::pystr::{
    is_lower, is_upper, line_count, lstrip, py_stem, rstrip, splitlines,
};

#[test]
fn pystr_splitlines_covers_every_boundary() {
    assert_eq!(splitlines(""), Vec::<&str>::new());
    assert_eq!(splitlines("a"), vec!["a"]);
    assert_eq!(splitlines("a\n"), vec!["a"]);
    assert_eq!(splitlines("a\r\nb\rc"), vec!["a", "b", "c"]);
    assert_eq!(splitlines("a\u{b}b\u{c}c"), vec!["a", "b", "c"]);
    assert_eq!(splitlines("a\x1cb\x1dc\x1e"), vec!["a", "b", "c"]);
    assert_eq!(splitlines("ab"), vec!["a", "b"]);
    assert_eq!(splitlines("a b c"), vec!["a", "b", "c"]);
    assert_eq!(line_count("a\nb\n"), 2);
    assert_eq!(line_count(""), 0);
}

#[test]
fn pystr_strips_python_whitespace() {
    assert_eq!(lstrip(" \x1c x "), "x ");
    assert_eq!(rstrip(" x \x1f "), " x");
    assert_eq!(lstrip(""), "");
}

#[test]
fn pystr_stems_like_pathlib() {
    assert_eq!(py_stem("notes.txt"), "notes");
    assert_eq!(py_stem("archive.tar.gz"), "archive.tar");
    assert_eq!(py_stem(".bashrc"), ".bashrc");
    assert_eq!(py_stem("noext"), "noext");
    assert_eq!(py_stem("/a/b/c.md"), "c");
}

#[test]
fn pystr_case_verdicts() {
    assert!(is_upper('A') && !is_lower('A'));
    assert!(is_lower('a') && !is_upper('a'));
    assert!(!is_upper('5') && !is_lower('5'));
    // A non-Lu letter that still counts as upper.
    assert!(is_upper('\u{2160}') && !is_lower('\u{2160}'));
    // Titlecase is neither either.
    assert!(!is_upper('\u{01C5}') && !is_lower('\u{01C5}'));
    assert!(is_lower('\u{DF}'));
}

// --- lib helpers ---

#[test]
fn lib_decoders_suffixes_and_sections() {
    assert_eq!(marginalia_parse::decode_replace(b"a\xffb"), "a\u{fffd}b");
    assert!(marginalia_parse::decode_strict(b"\xff").is_err());
    assert_eq!(marginalia_parse::decode_strict(b"ok").unwrap(), "ok");
    assert_eq!(marginalia_parse::lower_suffix("A.HTML"), ".html");
    assert_eq!(marginalia_parse::lower_suffix("archive.tar.gz"), ".gz");
    assert_eq!(marginalia_parse::lower_suffix(".bashrc"), "");
    assert_eq!(marginalia_parse::lower_suffix("noext"), "");
    assert_eq!(marginalia_parse::lower_suffix("/A/B.C"), ".c");
    assert_eq!(marginalia_parse::translate_newlines("a\r\nb\rc"), "a\nb\nc");
    let full = marginalia_parse::Section {
        char_start: 0,
        char_end: 5,
        heading: Some("H".to_owned()),
        level: Some(2),
        href: Some("c.xhtml".to_owned()),
    };
    let map = marginalia_parse::section_map(&full);
    assert_eq!(map.len(), 5);
    let bare = marginalia_parse::Section {
        char_start: 0,
        char_end: 5,
        heading: None,
        level: None,
        href: None,
    };
    assert_eq!(marginalia_parse::section_map(&bare).len(), 2);
}

// --- normalize_entities ---

use marginalia_parse::normalize_entities::{normalize_entities, normalize_nav_entities};

#[test]
fn entities_standard_spellings_pass_through() {
    assert_eq!(
        normalize_entities("<p>a&amp;b&#65;&#x42;</p>").unwrap(),
        "<p>a&amp;b&#65;&#x42;</p>"
    );
}

#[test]
fn entities_unknown_names_lose_their_semicolon() {
    assert_eq!(
        normalize_entities("<p>&bogus;!</p>").unwrap(),
        "<p>&bogus!</p>"
    );
    assert_eq!(normalize_entities("<p>&u;;</p>").unwrap(), "<p>&u&#59;</p>");
}

#[test]
fn entities_legacy_prefixes_are_blocked() {
    assert_eq!(
        normalize_entities("<p>&lt3</p>").unwrap(),
        "<p>&#38;lt3</p>"
    );
    assert_eq!(
        normalize_entities("<p>&amp-x</p>").unwrap(),
        "<p>&#38;amp-x</p>"
    );
}

#[test]
fn entities_malformed_references_fail() {
    assert!(normalize_entities("<p>A&#38b</p>").is_err());
    assert_eq!(normalize_entities("<p>&#x4z</p>").unwrap(), "<p>&#x4z</p>");
}

#[test]
fn entities_literal_positions_pass_through() {
    assert_eq!(
        normalize_entities("<p>A&# B&#x C&#</p>").unwrap(),
        "<p>A&# B&#x C&#</p>"
    );
    assert_eq!(
        normalize_entities("<p>& ;&1;&</p>").unwrap(),
        "<p>& ;&1;&</p>"
    );
}

#[test]
fn entities_raw_regions_are_untouched() {
    assert_eq!(
        normalize_entities("<!--&amp;--><script>a&amp;b</script><style>c&lt;d</style>").unwrap(),
        "<!--&amp;--><script>a&amp;b</script><style>c&lt;d</style>"
    );
    assert_eq!(
        normalize_entities("<title>a&amp;b</title><textarea>x&lt;y</textarea>").unwrap(),
        "<title>a&amp;b</title><textarea>x&lt;y</textarea>"
    );
    assert_eq!(
        normalize_entities("<!DOCTYPE html><p>a</p><?pi &amp; ?>").unwrap(),
        "<!DOCTYPE html><p>a</p><?pi &amp; ?>"
    );
    assert_eq!(
        normalize_entities("<!DOCTYPE a \"x>y\" z><p>x</p>").unwrap(),
        "<!DOCTYPE a \"x>y\" z><p>x</p>"
    );
}

#[test]
fn entities_title_close_needs_its_bracket() {
    assert_eq!(
        normalize_entities("<title>a</titlex>b</title>").unwrap(),
        "<title>a</titlex>b</title>"
    );
    assert_eq!(
        parse_html("<title>a</titlex>b</title><p>x</p>", "f.html")
            .title
            .as_deref(),
        Some("a</titlex>b")
    );
}

#[test]
fn entities_attributes_follow_unescape() {
    // Unknown keeps its semicolon; legacy without one is spelled out.
    assert_eq!(
        normalize_entities("<meta content=\"a&bogus;b\">").unwrap(),
        "<meta content=\"a&bogus;b\">"
    );
    assert_eq!(
        normalize_entities("<meta content=\"x&copy\">").unwrap(),
        "<meta content=\"x&copy;\">"
    );
    assert_eq!(
        normalize_entities("<meta content=\"a&ampb\">").unwrap(),
        "<meta content=\"a&ampb\">"
    );
    assert_eq!(normalize_entities("<a disabled>").unwrap(), "<a disabled>");
    assert_eq!(normalize_entities("<a href=x/>").unwrap(), "<a href=x/>");
    assert_eq!(normalize_entities("<p>a</p").unwrap(), "<p>a</p");
}

#[test]
fn nav_entities_follow_libxml2() {
    assert_eq!(
        normalize_nav_entities("<p>&copy; &#59;</p>"),
        "<p>&copy; &#59;</p>"
    );
    assert_eq!(normalize_nav_entities("<p>&amp E</p>"), "<p>&#38;amp E</p>");
    assert_eq!(normalize_nav_entities("<p>&lt3</p>"), "<p>&#38;lt3</p>");
    assert_eq!(normalize_nav_entities("<p>&#;x</p>"), "<p>x</p>");
    assert_eq!(
        normalize_nav_entities("<p>&bogus; &amp;</p>"),
        "<p>&bogus; &amp;</p>"
    );
    assert_eq!(
        normalize_nav_entities("<script>&amp;</script><p>&amp;</p>"),
        "<script>&amp;</script><p>&amp;</p>"
    );
}

// --- baked tables pin their source ---

#[test]
fn table_sizes_pin_the_unicode_version() {
    assert_eq!(marginalia_parse::case_tables::UPPER_RANGES.len(), 651);
    assert_eq!(marginalia_parse::case_tables::LOWER_RANGES.len(), 671);
    assert_eq!(marginalia_parse::html_entities::HTML_ENTITIES.len(), 2125);
    assert_eq!(marginalia_parse::html_entities::HTML5_ENTITIES.len(), 2231);
}

#[test]
fn entity_lookups() {
    use marginalia_parse::html_entities::{html5_entity, html_entity};
    assert_eq!(html_entity("amp"), Some("&"));
    assert_eq!(html_entity("not"), Some("\u{ac}"));
    assert_eq!(html_entity("notit"), None);
    assert_eq!(html_entity("fjlig"), Some("fj"));
    assert_eq!(html5_entity("amp;"), Some("&"));
    assert_eq!(html5_entity("amp="), None);
}
// --- gap closers: every uncovered branch, each a behavior ---

/// A fully explicit EPUB: container, OPF, and files as given.
fn raw_epub(container: &str, opf: &str, files: &[(&str, &[u8])]) -> Vec<u8> {
    use std::io::Write;
    let mut buf = Vec::new();
    {
        let mut zip = zip::ZipWriter::new(std::io::Cursor::new(&mut buf));
        let opts = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Stored);
        let mut add = |name: &str, content: &[u8]| {
            zip.start_file(name, opts).unwrap();
            zip.write_all(content).unwrap();
        };
        add("META-INF/container.xml", container.as_bytes());
        add("EPUB/content.opf", opf.as_bytes());
        for (name, content) in files {
            add(name, content);
        }
        zip.finish().unwrap();
    }
    buf
}

const CONTAINER: &str = "<?xml version=\"1.0\"?><container \
     xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">\
     <rootfiles><rootfile media-type=\"application/oebps-package+xml\" \
     full-path=\"EPUB/content.opf\"/></rootfiles></container>";

fn chapter_file(heading: &str, body: &str) -> Vec<u8> {
    format!("<html><body><h1>{heading}</h1><p>{body}</p></body></html>").into_bytes()
}

#[test]
fn epub_dangling_spine_and_nav_skip() {
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"nav.xhtml\" id=\"nav\" media-type=\"application/xhtml+xml\" properties=\"nav\"/>\
        <junk/></manifest>\
        <spine><itemref idref=\"ghost\"/><itemref idref=\"c1\"/><itemref idref=\"nav\"/></spine></package>";
    let bytes = raw_epub(
        CONTAINER,
        opf,
        &[
            ("EPUB/c1.xhtml", &chapter_file("H", "B")),
            ("EPUB/nav.xhtml", b"<html><body><nav epub:type=\"toc\"><ol><li><a href=\"c1.xhtml\">H</a></li></ol></nav></body></html>"),
        ],
    );
    let doc = epub::parse_bytes(&bytes, "t.epub").unwrap();
    assert_eq!(doc.metadata["chapter_count"], json!(1));
}

#[test]
fn epub_heading_fallback_without_any_table() {
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let bytes = raw_epub(
        CONTAINER,
        opf,
        &[("EPUB/c1.xhtml", &chapter_file("Solo", "B"))],
    );
    let doc = epub::parse_bytes(&bytes, "t.epub").unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("Solo"));
    assert_eq!(doc.sections[0]["level"], json!(1));
}

#[test]
fn epub_heading_fallback_levels_and_empties() {
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"c2.xhtml\" id=\"c2\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><itemref idref=\"c1\"/><itemref idref=\"c2\"/></spine></package>";
    let bytes = raw_epub(
        CONTAINER,
        opf,
        &[
            ("EPUB/c1.xhtml", b"<html><head><h1>Head</h1></head><body><h2>Sub <em>E</em></h2><p>B</p></body></html>"),
            ("EPUB/c2.xhtml", b"<html><body><p>no headings</p></body></html>"),
        ],
    );
    let doc = epub::parse_bytes(&bytes, "t.epub").unwrap();
    // A heading in the head is moved into the body by HTML parsing, and
    // both sides read it there: it outranks the body's own `h2`.
    assert_eq!(doc.sections[0]["heading"], json!("Head"));
    assert_eq!(doc.sections[0]["level"], json!(1));
    assert_eq!(doc.sections.len(), 2);
    assert!(doc.sections[1].get("heading").is_none());
    // Multi-string headings weld without spaces, like `get_text` welds them.
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let body = b"<html><body><h2>Sub <em>E</em> x</h2></body></html>";
    let doc = epub::parse_bytes(
        &raw_epub(CONTAINER, opf, &[("EPUB/c1.xhtml", body)]),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("SubEx"));
    assert_eq!(doc.sections[0]["level"], json!(2));
    // Comments inside a heading vanish from its label.
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let body = b"<html><body><h1>A<!--c-->B</h1></body></html>";
    let doc = epub::parse_bytes(
        &raw_epub(CONTAINER, opf, &[("EPUB/c1.xhtml", body)]),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("AB"));
}

#[test]
fn epub_metadata_quirks() {
    // A literal `author` key (never `creator`), markup-led titles, a
    // comment, and a processing instruction that fails the parse.
    let opf = |meta: &str| {
        format!(
            "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
             <metadata>{meta}</metadata><manifest/>\
             <spine><itemref idref=\"c1\"/></spine></package>"
        )
    };
    let page: &[u8] = b"<html><body><p>x</p></body></html>";
    let files: Vec<(&str, &[u8])> = vec![("EPUB/c1.xhtml", page)];
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            &opf("<author xmlns=\"http://purl.org/dc/elements/1.1/\">A U Thor</author><!--c-->"),
            &files,
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.metadata["author"], json!("A U Thor"));
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            &opf("<title xmlns=\"http://purl.org/dc/elements/1.1/\"><b>x</b></title>"),
            &files,
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.title.as_deref(), Some("t"));
    assert!(epub::parse_bytes(&raw_epub(CONTAINER, &opf("<?p x?>"), &files), "t.epub").is_err());
    // No metadata element at all still parses.
    let bare = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <manifest/><spine/></package>";
    assert!(epub::parse_bytes(&raw_epub(CONTAINER, bare, &[]), "t.epub").is_ok());
}

#[test]
fn epub_manifest_quirks() {
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <foo/><item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"i.jpg\" id=\"i\" media-type=\"image/jpg\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let bytes = raw_epub(
        CONTAINER,
        opf,
        &[
            ("EPUB/c1.xhtml", &chapter_file("H", "B")),
            ("EPUB/i.jpg", b"fake"),
        ],
    );
    assert!(epub::parse_bytes(&bytes, "t.epub").is_ok());
}

#[test]
fn epub_ncx_quirks() {
    let ncx = |inner: &str| {
        format!(
            "<?xml version=\"1.0\"?><ncx xmlns=\"http://www.daisy.org/z3986/2005/ncx/\">\
             <navMap>{inner}</navMap></ncx>"
        )
    };
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"toc.ncx\" id=\"ncx\" media-type=\"application/x-dtbncx+xml\"/>\
        </manifest><spine toc=\"ncx\"><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    // A bare label with no element inside reads as no label upstream too:
    // the point keeps no title and the chapter falls back to its own heading.
    // An empty map reads the same way.
    for toc in ["<navPoint><navLabel>bare</navLabel></navPoint>", ""] {
        let doc = epub::parse_bytes(
            &raw_epub(
                CONTAINER,
                opf,
                &[
                    ("EPUB/c1.xhtml", ch.as_slice()),
                    ("EPUB/toc.ncx", ncx(toc).as_bytes()),
                ],
            ),
            "t.epub",
        )
        .unwrap();
        assert_eq!(doc.text, "H\nB");
        assert_eq!(doc.sections[0]["heading"], json!("H"));
        assert_eq!(doc.sections[0]["level"], json!(1));
    }
    // Nested points nest; fragments strip; empties stay out.
    let nested = ncx(
        "<navPoint><navLabel><text>Part</text></navLabel><content src=\"c1.xhtml#frag\"/>\
         <navPoint><navLabel><text></text></navLabel><content src=\"c1.xhtml\"/></navPoint></navPoint>",
    );
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf,
            &[
                ("EPUB/c1.xhtml", ch.as_slice()),
                ("EPUB/toc.ncx", nested.as_bytes()),
            ],
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("Part"));
    assert_eq!(doc.sections[0]["level"], json!(1));
}

#[test]
fn epub_nav_quirks() {
    let nav = |inner: &str| {
        format!("<html><body><nav epub:type=\"toc\"><ol>{inner}</ol></nav></body></html>")
            .into_bytes()
    };
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"nav.xhtml\" id=\"nav\" media-type=\"application/xhtml+xml\" properties=\"nav\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    // A list item whose link sits a level down still nests: the outer
    // section takes the inner link's address, and a link without an href
    // never entries.
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf,
            &[("EPUB/c1.xhtml", ch.as_slice()), ("EPUB/nav.xhtml", &nav("<li><ol><li><a href=\"c1.xhtml\">Deep</a></li></ol></li><li><a>No href</a></li>"))],
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("Deep"));
    assert_eq!(doc.sections[0]["level"], json!(2));
    assert_eq!(doc.sections[0]["href"], json!("c1.xhtml"));
    // A nav without a toc list reads as no table upstream: the chapter
    // falls back to its own heading instead of failing.
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf,
            &[
                ("EPUB/c1.xhtml", ch.as_slice()),
                (
                    "EPUB/nav.xhtml",
                    b"<html><body><nav><ol></ol></nav></body></html>",
                ),
            ],
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("H"));
    // A page list without its list fails upstream too (inside its own
    // parser), so this failure stands.
    assert!(
        epub::parse_bytes(
            &raw_epub(
                CONTAINER,
                opf,
                &[("EPUB/c1.xhtml", ch.as_slice()), ("EPUB/nav.xhtml", b"<html><body><nav epub:type=\"toc\"><ol><li><a href=\"c1.xhtml\">H</a></li></ol></nav><nav epub:type=\"page-list\"></nav></body></html>")],
            ),
            "t.epub"
        )
        .is_err()
    );
    // Comments and nested markup in titles read as text.
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf,
            &[
                ("EPUB/c1.xhtml", ch.as_slice()),
                (
                    "EPUB/nav.xhtml",
                    &nav("<li><a href=\"c1.xhtml\">A<!--c-->B <em>C</em></a></li>"),
                ),
            ],
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("AB C"));
}

#[test]
fn epub_flatten_skips() {
    assert_eq!(epub::posix_normpath("a/../../b"), "../b");
    assert_eq!(epub::posix_normpath("a"), "a");
    assert_eq!(epub::posix_normpath("/"), "/");
    assert_eq!(marginalia_parse::lower_suffix("x"), "");
}

#[test]
fn epub_chapter_cdata_reads_raw() {
    // Chapters read the whole soup: CDATA content arrives verbatim.
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    // A chapter of pure CDATA reads empty: libxml2's rebuild parses the
    // section as a bogus comment, so no text survives it.
    let body = b"<html><body><p><![CDATA[a&amp;b]]></p></body></html>";
    let doc = epub::parse_bytes(
        &raw_epub(CONTAINER, opf, &[("EPUB/c1.xhtml", body)]),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.text, "");
    assert!(doc.sections.is_empty());
}

#[test]
fn html_cdata_drops_with_comments() {
    // The module's exact-type walk never sees CDATA sections.
    let doc = parse_html(
        "<body><p><![CDATA[a&amp;b<c]]></p><p>after</p></body>",
        "c.html",
    );
    assert_eq!(doc.text, "after");
}

#[test]
fn html_template_and_empty_marks() {
    let doc = parse_html(
        "<body><template><h1>T</h1><p>x</p></template><h1>U</h1></body>",
        "t.html",
    );
    assert_eq!(doc.text, "U");
    assert_eq!(doc.sections.len(), 1);
    assert_eq!(doc.sections[0]["heading"], json!("U"));
    assert_eq!(doc.metadata["heading_count"], json!(2));
    let doc = parse_html("<h1></h1>", "e.html");
    assert!(doc.sections.is_empty());
}

#[test]
fn html_body_scan_edges() {
    // Unterminated constructs run to the end of input.
    assert!(!html::has_explicit_body("<!--<body>"));
    assert!(!html::has_explicit_body("<script><body>"));
    assert!(!html::has_explicit_body("<a href=\"x"));
    assert!(html::has_explicit_body("<a href=\"x\">y</a><body>"));
    assert!(!html::has_explicit_body("<!doctype <body>"));
    // Bodies always synthesize, even for fragments and empty input.
    let doc = parse_html("", "e.html");
    assert_eq!(doc.text, "");
}

#[test]
fn tei_own_text_and_dates() {
    // Loose div text and tails belong to the div.
    let doc = tei::parse_bytes(
        "<TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><text><body>\
         <div>loose<head>H</head><p>a</p>tail</div></body></text></TEI>"
            .as_bytes(),
        "t.xml",
    )
    .unwrap();
    assert!(doc.text.contains("loose") && doc.text.contains("tail"));
    // A section with no prose of its own dissolves into its child.
    let doc = tei::parse_bytes(
        "<TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><text><body>\
         <div><head>H</head><div><head>C</head><p>x</p></div></div></body></text></TEI>"
            .as_bytes(),
        "t.xml",
    )
    .unwrap();
    assert_eq!(doc.sections.len(), 2);
    // Dates without `when` read their text.
    let doc = tei::parse_bytes(
        "<TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><teiHeader><fileDesc><titleStmt>\
         <title>D</title></titleStmt><publicationStmt><date>long ago</date>\
         </publicationStmt></fileDesc></teiHeader><text><body><p>x</p></body></text></TEI>"
            .as_bytes(),
        "d.xml",
    )
    .unwrap();
    assert_eq!(doc.metadata["date"], json!("long ago"));
}

#[test]
fn pdf_info_paths() {
    let dir = env!("CARGO_MANIFEST_DIR");
    let info = std::fs::read(format!("{dir}/tests/fixtures/info.pdf")).unwrap();
    let noinfo = std::fs::read(format!("{dir}/tests/fixtures/noinfo.pdf")).unwrap();
    assert_eq!(pdf::info_string(&info, "Title").unwrap(), "T");
    assert_eq!(pdf::info_string(&info, "Subject").unwrap(), "");
    assert_eq!(pdf::info_string(&info, "Missing").unwrap(), "");
    assert_eq!(pdf::info_string(&noinfo, "Title").unwrap(), "");
    assert_eq!(pdf::page_count(&info).unwrap(), 1);
    assert!(pdf::extract_pages(&info).is_ok());
    assert_eq!(pdf::pdf_string(b"\xff"), "\u{ff}");
}

#[test]
fn xml_branches() {
    // CDATA is raw text; prefixed attrs resolve; xmlns-prefix fails.
    let doc = parse_xml(b"<r><p><![CDATA[x&y]]></p></r>", TextModel::Lxml).unwrap();
    assert_eq!(doc.root.find_child("p").unwrap().text(), "x&y");
    assert!(parse_xml(b"<xmlns:a/>", TextModel::Lxml).is_err());
    let doc = parse_xml(
        "<r xmlns:p=\"urn:p\" xml:lang=\"en\"><p:a p:k=\"v\" xml:s=\"t\">x</p:a></r>".as_bytes(),
        TextModel::Lxml,
    )
    .unwrap();
    let el = &doc.root.findall_descendants("{urn:p}a")[0];
    assert_eq!(el.attr("k"), Some("v"));
    assert!(parse_xml(b"<r p:k=\"v\"/>", TextModel::Lxml).is_err());
    // Parameter entities declare markup, never text.
    assert!(parse_xml(
        "<!DOCTYPE r [<!ENTITY % pe \"v\">]><r><p>x</p></r>".as_bytes(),
        TextModel::Lxml
    )
    .is_ok());
    // `<?xml` alone never starts a declaration.
    assert!(parse_xml(b"<?xml", TextModel::Lxml).is_err());
    // A second top-level element fails, opened or shut.
    assert!(parse_xml(b"<a></a><b></b>", TextModel::Lxml).is_err());
    // Segments skip comments; tails after elements read.
    let doc = parse_xml(b"<r><p>a<!--c-->b</p></r>", TextModel::Lxml).unwrap();
    assert_eq!(
        doc.root.find_child("p").unwrap().text_segments(),
        vec!["a", "b"]
    );
    // A cyclic pair oscillates into the pass cap and fails.
    assert!(parse_xml(
        "<!DOCTYPE r [<!ENTITY a \"&b;\"><!ENTITY b \"&a;\">]><r><p>&a;</p></r>".as_bytes(),
        TextModel::Lxml
    )
    .is_err());
}

#[test]
fn entities_scanner_edges() {
    // Unterminated comment, declaration, tag, and quoted value run out.
    assert_eq!(normalize_entities("<!--foo").unwrap(), "<!--foo");
    assert_eq!(normalize_entities("<!foo").unwrap(), "<!foo");
    assert_eq!(normalize_entities("<a ").unwrap(), "<a ");
    assert_eq!(normalize_entities("<a href=\"x").unwrap(), "<a href=\"x");
    assert_eq!(normalize_entities("<1<<").unwrap(), "<1<<");
    assert_eq!(normalize_entities("<script>foo").unwrap(), "<script>foo");
    assert_eq!(normalize_entities("<a href= x>").unwrap(), "<a href=x>");
    assert_eq!(normalize_entities("<a @>").unwrap(), "<a @>");
    // CDATA sections drop with the comments: the module's exact-type
    // walk never sees them.
    assert_eq!(
        normalize_entities("<![CDATA[&amp;]]><p>x</p>").unwrap(),
        "<p>x</p>"
    );
    // A bare unknown name with no legacy prefix stands.
    assert_eq!(
        normalize_entities("<p>&xyz9 q</p>").unwrap(),
        "<p>&xyz9 q</p>"
    );
    // Numerics in attributes always decode; misses stand.
    assert_eq!(
        normalize_entities("<meta content=\"a&#65;b\">").unwrap(),
        "<meta content=\"a&#65;b\">"
    );
    assert_eq!(
        normalize_entities("<meta content=\"a&#x41;b\">").unwrap(),
        "<meta content=\"a&#x41;b\">"
    );
    assert_eq!(
        normalize_entities("<meta content=\"a& b\">").unwrap(),
        "<meta content=\"a& b\">"
    );
    // Nav: comments, stray brackets, bare tags, raw bodies.
    assert_eq!(normalize_nav_entities("<!--&amp;--><1"), "<!--&amp;--><1");
    assert_eq!(normalize_nav_entities("<a href=\"x"), "<a href=\"x");
    assert_eq!(normalize_nav_entities("<a href=x"), "<a href=x");
    assert_eq!(normalize_nav_entities("<?p \"a>b\"?>T"), "<?p \"a>b\"?>T");
    assert_eq!(normalize_nav_entities("<!D \"a"), "<!D \"a");
    assert_eq!(normalize_nav_entities("<?p \"a"), "<?p \"a");
    assert_eq!(normalize_nav_entities("<script>&amp;"), "<script>&amp;");
    assert_eq!(normalize_nav_entities("<p>&#38b</p>"), "<p>&#38b</p>");
}
// --- final gap closers ---

#[test]
fn epub_container_and_manifest_shapes() {
    // A non-package rootfile is skipped for the matching one.
    let container = "<?xml version=\"1.0\"?><container \
         xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\"><rootfiles>\
         <rootfile media-type=\"text/plain\" full-path=\"nope.txt\"/>\
         <rootfile media-type=\"application/oebps-package+xml\" full-path=\"EPUB/content.opf\"/>\
         </rootfiles></container>";
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><foo/><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    let doc = epub::parse_bytes(
        &raw_epub(container, opf, &[("EPUB/c1.xhtml", ch.as_slice())]),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.metadata["chapter_count"], json!(1));
    // `creator` is not `author`: the literal key stays empty.
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata><creator xmlns=\"http://purl.org/dc/elements/1.1/\">C</creator></metadata>\
        <manifest/><spine/></package>";
    let doc = epub::parse_bytes(&raw_epub(CONTAINER, opf, &[]), "t.epub").unwrap();
    assert!(doc.metadata.get("author").is_none());
    // No manifest element at all still parses.
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><spine/></package>";
    assert!(epub::parse_bytes(&raw_epub(CONTAINER, opf, &[]), "t.epub").is_ok());
    assert_eq!(marginalia_parse::lower_suffix("x"), "");
}

#[test]
fn epub_chapter_tails_and_comments() {
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    // Tails ride along; comments do not; nested markup nests.
    let body = b"<html><body><h1>H <em>E</em></h1>tail<!--c--><p>a<!--d-->b</p></body></html>";
    let doc = epub::parse_bytes(
        &raw_epub(CONTAINER, opf, &[("EPUB/c1.xhtml", body)]),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.text, "H\nE\ntail\na\nb");
}

#[test]
fn epub_template_heading_falls_through() {
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let body = b"<html><body><template><h1>T</h1></template><h2>U</h2></body></html>";
    let doc = epub::parse_bytes(
        &raw_epub(CONTAINER, opf, &[("EPUB/c1.xhtml", body)]),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("U"));
    assert_eq!(doc.sections[0]["level"], json!(2));
}

#[test]
fn epub_bare_list_items_never_enter() {
    let nav = "<html><body><nav epub:type=\"toc\"><ol><li>bare</li>        <li><a href=\"c1.xhtml\">Real</a></li></ol></nav></body></html>";
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"nav.xhtml\" id=\"nav\" media-type=\"application/xhtml+xml\" properties=\"nav\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf,
            &[
                ("EPUB/c1.xhtml", ch.as_slice()),
                ("EPUB/nav.xhtml", nav.as_bytes()),
            ],
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections.len(), 1);
    assert_eq!(doc.sections[0]["heading"], json!("Real"));
}

#[test]
fn epub_empty_href_links_never_enter() {
    let nav = "<html><body><nav epub:type=\"toc\"><ol>\
        <li><a href=\"\">Empty</a></li>\
        <li><a href=\"c1.xhtml\">Real</a></li></ol></nav></body></html>"
        .as_bytes();
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"nav.xhtml\" id=\"nav\" media-type=\"application/xhtml+xml\" properties=\"nav\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf,
            &[("EPUB/c1.xhtml", ch.as_slice()), ("EPUB/nav.xhtml", nav)],
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections.len(), 1);
    assert_eq!(doc.sections[0]["heading"], json!("Real"));
}

#[test]
fn html_heading_subtree_prune_and_empty_title() {
    let doc = parse_html("<h1><script>x</script>T<!--c--></h1>", "t.html");
    assert_eq!(doc.sections[0]["heading"], json!("T"));
    let doc = parse_html("<title></title><p>x</p>", "stem.html");
    assert_eq!(doc.title.as_deref(), Some("stem"));
    let doc = parse_html("<title>T</title>", "t.html");
    assert_eq!(doc.title.as_deref(), Some("T"));
}

#[test]
fn html_detect_and_lang_edges() {
    assert_eq!(
        html::detect("page.txt", None),
        (0.0, "not detected as HTML".to_owned())
    );
    let doc = parse_html(
        "<html lang=\"fr\"><head></head><body><p>x</p></body></html>",
        "t.html",
    );
    assert_eq!(doc.language.as_deref(), Some("fr"));
    assert_eq!(doc.metadata["language"], json!("fr"));
}

#[test]
fn tei_detect_and_wrapper_edges() {
    assert_eq!(
        tei::detect("doc.xml", None),
        (0.0, "not detected as TEI XML".to_owned())
    );
    // A wrapper div with no prose of its own dissolves into its child.
    let doc = tei::parse_bytes(
        "<TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><text><body>\
         <div><div><head>C</head><p>x</p></div></div></body></text></TEI>"
            .as_bytes(),
        "t.xml",
    )
    .unwrap();
    assert_eq!(doc.sections.len(), 1);
    assert_eq!(doc.sections[0]["heading"], json!("C"));
}

#[test]
fn pdf_dangling_info() {
    let dir = env!("CARGO_MANIFEST_DIR");
    let raw = std::fs::read(format!("{dir}/tests/fixtures/info_broken.pdf")).unwrap();
    assert_eq!(pdf::info_string(&raw, "Title").unwrap(), "");
    assert_eq!(pdf::page_count(&raw).unwrap(), 1);
}

#[test]
fn xml_nested_branches() {
    // Nested itertext descends; `xml:` binds without declaration.
    let doc = parse_xml(b"<r><a>x<b>y</b></a></r>", TextModel::Lxml).unwrap();
    assert_eq!(doc.root.find_child("a").unwrap().itertext(), "xy");
    let doc = parse_xml(b"<xml:a/>", TextModel::Lxml).unwrap();
    assert_eq!(doc.root.ns_uri, "http://www.w3.org/XML/1998/namespace");
    // A declared encoding with undecodable bytes fails, like upstream.
    assert!(parse_xml(
        b"<?xml version=\"1.0\" encoding=\"ascii\"?><r><p>caf\xe9</p></r>",
        TextModel::Lxml
    )
    .is_err());
    // `<?xml` alone never starts a declaration: strict UTF-8 then fails it.
    assert!(parse_xml(b"<?xml", TextModel::Lxml).is_err());
    // A doctype after the root is out of place; a second top-level start fails.
    assert!(parse_xml(b"<r/>\n<!DOCTYPE x>", TextModel::Lxml).is_err());
    // Exponential entity growth trips the length guard.
    let mut defs = String::new();
    defs.push_str("<!ENTITY x0 \"0123456789\">");
    for level in 1..28 {
        defs.push_str(&format!(
            "<!ENTITY x{level} \"&x{};&x{};\"/>",
            level - 1,
            level - 1
        ));
    }
    // Self-closing to stay well-formed past the definitions.
    let doc = format!("<!DOCTYPE r [{defs}]><r><p>&x27;</p></r>");
    assert!(parse_xml(doc.as_bytes(), TextModel::Lxml).is_err());
}

#[test]
fn entities_tag_quote_and_nav_edges() {
    assert_eq!(
        normalize_entities("<a title=\"a>b\">x</a>").unwrap(),
        "<a title=\"a>b\">x</a>"
    );
    assert_eq!(normalize_entities("<a href= x>").unwrap(), "<a href=x>");
    assert_eq!(
        normalize_nav_entities("<p>&#38b &1 &</p>"),
        "<p>&#38b &1 &</p>"
    );
    assert_eq!(
        normalize_nav_entities("<nav><a title=\"a>b\" href=\"u\">T</a></nav>"),
        "<nav><a title=\"a>b\" href=\"u\">T</a></nav>"
    );
    assert_eq!(normalize_entities("&#65").unwrap(), "&#65");
}

#[test]
fn tag_string_contract() {
    // The helper's contract on non-title shapes: one string child reads,
    // any other shape refuses.
    let doc = scraper::Html::parse_document("<div><p>x</p></div><span>s</span>");
    let sel = scraper::Selector::parse("div,span").unwrap();
    let mut found = vec![];
    for el in doc.select(&sel) {
        found.push(marginalia_parse::html::tag_string(el));
    }
    assert_eq!(found, vec![None, Some("s".to_owned())]);
}

#[test]
fn html_bodies_always_synthesize() {
    // The proof the missing-body branches stand on: selection succeeds
    // even for fragments and empty input.
    for src in ["", "<p>x", "just text"] {
        let _ = parse_html(src, "f.html");
    }
}
// --- last gap closers ---

#[test]
fn epub_pages_and_container_edges() {
    // A valid page list parses and is dropped; the counts still print.
    let nav = "<html><body><nav epub:type=\"toc\"><ol>\
        <li><a href=\"c1.xhtml\">H</a></li></ol></nav>\
        <nav epub:type=\"page-list\"><ol><li><a href=\"c1.xhtml\">1</a></li></ol></nav></body></html>";
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"nav.xhtml\" id=\"nav\" media-type=\"application/xhtml+xml\" properties=\"nav\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf,
            &[
                ("EPUB/c1.xhtml", ch.as_slice()),
                ("EPUB/nav.xhtml", nav.as_bytes()),
            ],
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("H"));
}

#[test]
fn entities_close_and_grit_edges() {
    // Whitespace before a raw-text close is skipped on both sides.
    assert_eq!(
        normalize_entities("<title>a</title  >").unwrap(),
        "<title>a</title  >"
    );
    assert_eq!(
        normalize_entities("<script>a</script\t>").unwrap(),
        "<script>a</script\t>"
    );
    // Grit between attributes is literal and invisible either way.
    assert_eq!(normalize_entities("<a / >").unwrap(), "<a / >");
    // Spaces around `=` never reach either DOM.
    assert_eq!(normalize_entities("<a href =x>").unwrap(), "<a href=x>");
    // A non-legacy bare name stands in nav text too.
    assert_eq!(normalize_nav_entities("<p>&xyz9 q</p>"), "<p>&xyz9 q</p>");
}

#[test]
fn xml_declared_ascii_ok_path() {
    assert!(parse_xml(
        b"<?xml version=\"1.0\" encoding=\"us-ascii\"?><r><p>plain</p></r>",
        TextModel::Lxml
    )
    .is_ok());
}
// --- truly-final gap closers ---

#[test]
fn epub_missing_spine_fails() {
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest/></package>";
    assert!(epub::parse_bytes(&raw_epub(CONTAINER, opf, &[]), "t.epub").is_err());
}

#[test]
fn epub_ncx_junk_and_labelless_branches() {
    let ncx = "<?xml version=\"1.0\"?><ncx xmlns=\"http://www.daisy.org/z3986/2005/ncx/\">\
        <navMap><junk/><navPoint><content src=\"c1.xhtml\"/>\
        <navPoint><content src=\"c1.xhtml\"/></navPoint>\
        <foo/></navPoint></navMap></ncx>";
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"toc.ncx\" id=\"ncx\" media-type=\"application/x-dtbncx+xml\"/>\
        </manifest><spine toc=\"ncx\"><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf,
            &[
                ("EPUB/c1.xhtml", ch.as_slice()),
                ("EPUB/toc.ncx", ncx.as_bytes()),
            ],
        ),
        "t.epub",
    )
    .unwrap();
    // Labelless points contribute no titles at all, so the chapter falls
    // back to its own markup heading.
    assert_eq!(doc.sections[0]["heading"], json!("H"));
    assert_eq!(doc.sections[0]["level"], json!(1));
}

#[test]
fn epub_empty_heading_falls_to_the_next() {
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let body = b"<html><body><h1>   </h1><h2>Real</h2></body></html>";
    let doc = epub::parse_bytes(
        &raw_epub(CONTAINER, opf, &[("EPUB/c1.xhtml", body)]),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("Real"));
    assert_eq!(doc.sections[0]["level"], json!(2));
}

#[test]
fn xml_declared_windows_encoding_strictness() {
    assert!(parse_xml(
        b"<?xml version=\"1.0\" encoding=\"windows-1252\"?><r><p>\x81</p></r>",
        TextModel::Lxml
    )
    .is_err());
    assert!(parse_xml(
        b"<?xml version=\"1.0\" encoding=\"windows-1252\"?><r><p>caf\xe9</p></r>",
        TextModel::Lxml
    )
    .is_ok());
}

#[test]
fn xml_declared_utf8_strictness() {
    assert!(parse_xml(
        b"<?xml version=\"1.0\" encoding=\"utf-8\"?><r><p>caf\xe9</p></r>",
        TextModel::Lxml
    )
    .is_err());
}

#[test]
fn xml_parameter_entity_without_space() {
    assert!(parse_xml(
        "<!DOCTYPE r [<!ENTITY %foo \"bar\">]><r><p>x</p></r>".as_bytes(),
        TextModel::Lxml
    )
    .is_ok());
}
// --- error matrix: every failure fails at its own site ---

#[track_caller]
fn parse_err(bytes: &[u8], name: &str) -> String {
    match epub::parse_bytes(bytes, name) {
        Err(marginalia_types::Error::Parse(msg)) => msg,
        other => panic!("expected a parse failure, got {other:?}"),
    }
}

#[test]
fn epub_error_matrix() {
    let ch = chapter_file("H", "B");
    let opf = |manifest: &str, spine: &str| {
        format!(
            "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
             <metadata></metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>"
        )
    };
    let item = "<item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>";
    // Not a zip at all.
    assert!(parse_err(b"not a zip", "t.epub").starts_with("not a ZIP archive"));
    // A zip without a container.
    let mut buf = Vec::new();
    {
        use std::io::Write;
        let mut zip = zip::ZipWriter::new(std::io::Cursor::new(&mut buf));
        zip.start_file("EPUB/content.opf", zip::write::SimpleFileOptions::default())
            .unwrap();
        zip.write_all(b"<package/>").unwrap();
        zip.finish().unwrap();
    }
    assert_eq!(
        parse_err(&buf, "t.epub"),
        "cannot find META-INF/container.xml in archive"
    );
    // The container points nowhere.
    let container = CONTAINER.replace("EPUB/content.opf", "EPUB/missing.opf");
    assert_eq!(
        parse_err(
            &raw_epub(
                &container,
                &opf(item, ""),
                &[("EPUB/c1.xhtml", ch.as_slice())]
            ),
            "t.epub"
        ),
        "cannot find EPUB/missing.opf in archive"
    );
    // A chapter file the manifest names but the archive lacks.
    assert_eq!(
        parse_err(
            &raw_epub(CONTAINER, &opf(item, "<itemref idref=\"c1\"/>"), &[]),
            "t.epub"
        ),
        "cannot find EPUB/c1.xhtml in archive"
    );
    // A chapter whose markup carries a dying reference fails in place.
    let crash = b"<html><body><p>A&#38b</p></body></html>";
    let msg = parse_err(
        &raw_epub(
            CONTAINER,
            &opf(item, "<itemref idref=\"c1\"/>"),
            &[("EPUB/c1.xhtml", crash)],
        ),
        "t.epub",
    );
    assert!(
        msg.starts_with("malformed character reference at byte"),
        "{msg}"
    );
    // Corrupt entry data fails the read, not the lookup.
    let mut bytes = raw_epub(
        CONTAINER,
        &opf(item, "<itemref idref=\"c1\"/>"),
        &[("EPUB/c1.xhtml", ch.as_slice())],
    );
    let pos = bytes.windows(3).position(|w| w == b"<h1").unwrap();
    bytes[pos + 1] = b'X';
    assert!(
        parse_err(&bytes, "t.epub").starts_with("cannot read EPUB/c1.xhtml:"),
        "crc break must fail the read"
    );
}

#[test]
fn epub_dc_extras() {
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata xmlns:dc=\"http://purl.org/dc/elements/1.1/\">\
        <dc:title>Extra</dc:title><dc:language>he</dc:language>\
        <dc:publisher>P</dc:publisher><dc:date>2020</dc:date>\
        <dc:description>D</dc:description><dc:identifier>I</dc:identifier>\
        </metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    let doc = epub::parse_bytes(
        &raw_epub(CONTAINER, opf, &[("EPUB/c1.xhtml", ch.as_slice())]),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.title.as_deref(), Some("Extra"));
    assert_eq!(doc.language.as_deref(), Some("he"));
    assert_eq!(doc.metadata["dc_publisher"], json!("P"));
    assert_eq!(doc.metadata["dc_date"], json!("2020"));
    assert_eq!(doc.metadata["dc_description"], json!("D"));
    assert_eq!(doc.metadata["dc_identifier"], json!("I"));
}
// --- absolutely-final gap closers ---

#[test]
fn epub_non_dc_and_leading_text() {
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata><meta property=\"x\"/><foo/></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    // The body's own leading text stays behind; tails ride along.
    let body = b"<html><body>loose<p>x</p></body></html>";
    let doc = epub::parse_bytes(
        &raw_epub(CONTAINER, opf, &[("EPUB/c1.xhtml", body)]),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.text, "x");
    assert!(doc.metadata.get("author").is_none());
}

#[test]
fn epub_empty_toc_nav() {
    // A valid but empty table: nothing addresses, everything falls back.
    let nav = "<html><body><nav epub:type=\"toc\"><ol></ol></nav></body></html>";
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"nav.xhtml\" id=\"nav\" media-type=\"application/xhtml+xml\" properties=\"nav\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf,
            &[
                ("EPUB/c1.xhtml", ch.as_slice()),
                ("EPUB/nav.xhtml", nav.as_bytes()),
            ],
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("H"));
}

#[test]
fn epub_dirname_edges() {
    assert_eq!(epub::posix_dirname("/x"), "/");
    assert_eq!(epub::posix_dirname("x"), "");
    assert_eq!(epub::posix_dirname("a/b"), "a");
}

#[test]
fn html_unknown_meta_names_are_ignored() {
    let doc = parse_html(
        "<head><meta name=\"keywords\" content=\"k\"><meta></head><body><p>x</p></body>",
        "t.html",
    );
    assert!(doc.metadata.get("keywords").is_none());
    assert_eq!(doc.text, "x");
}

#[test]
fn html_meta_language_wins() {
    let doc = parse_html(
        "<html lang=\"fr\"><head><meta name=\"language\" content=\"es\"></head>\
         <body><p>x</p></body></html>",
        "t.html",
    );
    assert_eq!(doc.metadata["language"], json!("es"));
    assert_eq!(doc.language.as_deref(), Some("es"));
}

#[test]
fn entities_quoted_and_bare_edges() {
    assert_eq!(
        normalize_entities("<a title=\"a>b\">x</a>").unwrap(),
        "<a title=\"a>b\">x</a>"
    );
    assert_eq!(
        normalize_entities("<meta content=\"x\" name=\"d\"/>").unwrap(),
        "<meta content=\"x\" name=\"d\"/>"
    );
    assert_eq!(normalize_entities("<p>A&#65</p>").unwrap(), "<p>A&#65</p>");
    assert_eq!(
        normalize_nav_entities("<!DOCTYPE x><?p q?><p>&xyz9 q</p>"),
        "<!DOCTYPE x><?p q?><p>&xyz9 q</p>"
    );
}

#[test]
fn xml_custom_attr_entity() {
    // Internal entities expand in attribute values too, like upstream.
    let doc = parse_xml(
        "<!DOCTYPE r [<!ENTITY q \"Q\">]><r><a k=\"a&q;b\">x</a></r>".as_bytes(),
        TextModel::Lxml,
    )
    .unwrap();
    assert_eq!(doc.root.find_child("a").unwrap().attr("k"), Some("aQb"));
}
// --- failure sites, one per fallible call ---

#[test]
fn epub_each_failure_fails_at_its_own_site() {
    let ch = chapter_file("H", "B");
    let opf = |manifest: &str, spine: &str| {
        format!(
            "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
             <metadata></metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>"
        )
    };
    let item = "<item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>";
    let files: Vec<(&str, &[u8])> = vec![("EPUB/c1.xhtml", ch.as_slice())];
    // Malformed container, spine, manifest, and chapter XML each fail parsing.
    assert!(
        parse_err(&raw_epub("<container", &opf(item, ""), &[]), "t.epub")
            .starts_with("malformed XML")
    );
    // A container without a matching rootfile.
    let container = CONTAINER.replace("application/oebps-package+xml", "text/plain");
    assert_eq!(
        parse_err(&raw_epub(&container, &opf(item, ""), &files), "t.epub"),
        "cannot find container file"
    );
    // Malformed OPF, NCX bytes, and nav bytes.
    assert!(
        parse_err(&raw_epub(CONTAINER, "<package>", &files), "t.epub").starts_with("malformed XML")
    );
    let ncx_opf = format!(
        "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        {item}<item href=\"toc.ncx\" id=\"ncx\" media-type=\"application/x-dtbncx+xml\"/>\
        </manifest><spine toc=\"ncx\"><itemref idref=\"c1\"/></spine></package>"
    );
    assert!(parse_err(
        &raw_epub(
            CONTAINER,
            &ncx_opf,
            &[("EPUB/c1.xhtml", ch.as_slice()), ("EPUB/toc.ncx", b"<ncx>")]
        ),
        "t.epub"
    )
    .starts_with("malformed XML"));
    // A missing NCX or nav file fails its read.
    assert_eq!(
        parse_err(
            &raw_epub(CONTAINER, &ncx_opf, &[("EPUB/c1.xhtml", ch.as_slice())]),
            "t.epub"
        ),
        "cannot find EPUB/toc.ncx in archive"
    );
    // A crash reference inside a chapter fails the chapter.
    let crash = b"<html><body><p>A&#38b</p></body></html>";
    let msg = parse_err(
        &raw_epub(
            CONTAINER,
            &opf(item, "<itemref idref=\"c1\"/>"),
            &[("EPUB/c1.xhtml", crash)],
        ),
        "t.epub",
    );
    assert!(
        msg.starts_with("malformed character reference at byte"),
        "{msg}"
    );
}

#[test]
fn xml_attribute_failures() {
    // Unterminated attribute values fail the attribute scan.
    assert!(parse_xml(b"<r a=\"x>", TextModel::Lxml).is_err());
    // Unknown references fail inside values too.
    assert!(parse_xml(b"<r a=\"&bogus;\">x</r>", TextModel::Lxml).is_err());
    // Single-quoted entity definitions expand like double-quoted ones.
    let doc = parse_xml(
        "<!DOCTYPE r [<!ENTITY q 'Q'>]><r><p>&q;</p></r>".as_bytes(),
        TextModel::Lxml,
    )
    .unwrap();
    assert_eq!(doc.root.find_child("p").unwrap().text(), "Q");
}

#[test]
fn tei_title_fallbacks() {
    // No header at all: the stem titles.
    let doc =
        tei::parse_bytes(b"<TEI><text><body><p>x</p></body></text></TEI>", "stem.xml").unwrap();
    assert_eq!(doc.title.as_deref(), Some("stem"));
}

// --- coverage-completion: every test below pins a failure or boundary edge
// against its Python outcome, probed during development. ---

#[test]
fn epub_ncx_absent_and_tocless_navs_fall_back() {
    let opf_ncx = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"toc.ncx\" id=\"ncx\" media-type=\"application/x-dtbncx+xml\"/>\
        </manifest><spine toc=\"ncx\"><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    // An NCX without a navMap reads as an empty table upstream: the chapter
    // falls back to its own heading instead of failing.
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf_ncx,
            &[
                ("EPUB/c1.xhtml", ch.as_slice()),
                (
                    "EPUB/toc.ncx",
                    b"<ncx xmlns=\"http://www.daisy.org/z3986/2005/ncx/\" version=\"2005-1\">\
                      <head/><docTitle><text>T</text></docTitle></ncx>",
                ),
            ],
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.text, "H\nB");
    assert_eq!(doc.sections[0]["heading"], json!("H"));
    assert_eq!(doc.sections[0]["level"], json!(1));
    // A toc nav without its list fails the parse upstream too, so this
    // failure stands.
    let opf_nav = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"nav.xhtml\" id=\"nav\" media-type=\"application/xhtml+xml\" properties=\"nav\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    assert!(epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf_nav,
            &[
                ("EPUB/c1.xhtml", ch.as_slice()),
                (
                    "EPUB/nav.xhtml",
                    b"<html><body><nav epub:type=\"toc\"><h1>Contents</h1></nav></body></html>",
                ),
            ],
        ),
        "t.epub",
    )
    .is_err());
}

#[test]
fn epub_nav_branch_and_empty_links() {
    let nav = |inner: &str| {
        format!("<html><body><nav epub:type=\"toc\"><ol>{inner}</ol></nav></body></html>")
            .into_bytes()
    };
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        <item href=\"nav.xhtml\" id=\"nav\" media-type=\"application/xhtml+xml\" properties=\"nav\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let ch = chapter_file("H", "B");
    // A branch carrying its own link keeps the link's title at the branch
    // depth; the fragment child addresses the same file and loses the
    // setdefault race — upstream flattens to the same entry.
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf,
            &[(
                "EPUB/nav.xhtml",
                &nav("<li><a href=\"c1.xhtml\">Ch1</a><ol><li><a href=\"c1.xhtml#s1\">S1</a></li></ol></li>"),
            ), ("EPUB/c1.xhtml", ch.as_slice())],
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("Ch1"));
    assert_eq!(doc.sections[0]["level"], json!(1));
    // A link with an empty title contributes no entry upstream either: the
    // chapter falls back to its own heading.
    let doc = epub::parse_bytes(
        &raw_epub(
            CONTAINER,
            opf,
            &[
                ("EPUB/c1.xhtml", ch.as_slice()),
                ("EPUB/nav.xhtml", &nav("<li><a href=\"c1.xhtml\"></a></li>")),
            ],
        ),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.sections[0]["heading"], json!("H"));
}

#[test]
fn epub_whitespace_between_blocks_reads_clean() {
    // A whitespace-only run between elements is not a piece: upstream drops
    // it from the joined text the same way.
    let opf = "<?xml version=\"1.0\"?><package xmlns=\"http://www.idpf.org/2007/opf\">\
        <metadata></metadata><manifest>\
        <item href=\"c1.xhtml\" id=\"c1\" media-type=\"application/xhtml+xml\"/>\
        </manifest><spine><itemref idref=\"c1\"/></spine></package>";
    let ch = b"<html><body><h1>H</h1>   <p>B</p></body></html>";
    let doc = epub::parse_bytes(
        &raw_epub(CONTAINER, opf, &[("EPUB/c1.xhtml", ch.as_slice())]),
        "t.epub",
    )
    .unwrap();
    assert_eq!(doc.text, "H\nB");
}

#[test]
fn html_whitespace_and_title_crash() {
    // Whitespace-only runs between blocks never reach the text: the module's
    // strip-and-skip drops them before joining.
    let doc = parse_html("<body><h1>H</h1>   <p>B</p></body>", "w.html");
    assert_eq!(doc.text, "H\nB");
    // A malformed character reference in the title fails the parse: the
    // builder raises on it before any text is read.
    assert!(html::parse_text(
        "<html><head><title>&#38b</title></head><body><p>x</p></body></html>",
        "t.html",
    )
    .is_err());
}

#[test]
fn entities_alphanumeric_tail_preencodes() {
    // A name cut by its semicolon with an alphanumeric tail must not glue
    // back together downstream: the head is spelled numerically so the
    // later pass reads `&uamp;` back, the way the module reads `&u;amp;`.
    assert_eq!(
        marginalia_parse::normalize_entities::normalize_entities("&u;amp;").unwrap(),
        "&#38;uamp;"
    );
}
#[test]
fn entities_bare_tail_blocks_prefix_decode() {
    // A glued name with nothing after its semicolon cannot reach the
    // alphanumeric arm: with no tail the head spells through the blocking
    // rule instead, so no prefix decodes downstream (`&ampx` reads back).
    assert_eq!(
        marginalia_parse::normalize_entities::normalize_entities("&ampx;").unwrap(),
        "&#38;ampx"
    );
}
#[test]
fn entities_unknown_tail_stays_literal() {
    // A name with no decodable prefix has nothing to block: the head
    // passes through bare, and the module reads `&xyz` back upstream too.
    assert_eq!(
        marginalia_parse::normalize_entities::normalize_entities("&xyz;").unwrap(),
        "&xyz"
    );
}

#[test]
fn pystr_splitlines_unicode_boundaries() {
    // U+2029 separates exactly like U+2028; neither survives in the pieces.
    assert_eq!(splitlines("a\u{2029}b\u{2028}c"), vec!["a", "b", "c"]);
}

#[test]
fn tei_nested_comment_errors() {
    // A comment nested inside a section fails the same walk the top-level
    // comment fails: upstream cannot read text off it either. Two levels,
    // so the failure also propagates through a recursive call.
    let err = tei::parse_bytes(
        b"<?xml version=\"1.0\"?><TEI xmlns=\"http://www.tei-c.org/ns/1.0\">\
          <text><body><div><div><p>a</p><!--c--></div></div></body></text></TEI>",
        "n.xml",
    )
    .unwrap_err();
    assert_eq!(
        err.to_string(),
        "parsing a document failed: malformed XML: text of a comment or processing instruction"
    );
}

#[test]
fn xml_declaration_edges() {
    // An unterminated declaration never yields an encoding upstream either.
    assert!(parse_xml(b"<?xml version='1.0' <r/>", TextModel::Lxml)
        .unwrap_err()
        .to_string()
        .starts_with("parsing a document failed: malformed XML"));
    // Non-UTF-8 declaration bytes read as no declaration: the strict body
    // read then fails, the way the undecodable input fails upstream.
    assert!(parse_xml(b"<?xml \xff\xfe?><r/>", TextModel::Lxml)
        .unwrap_err()
        .to_string()
        .starts_with("parsing a document failed: not valid UTF-8"));
    // Single-quoted declarations read like double-quoted ones upstream.
    let doc = parse_xml(
        b"<?xml version='1.0' encoding='ISO-8859-1'?><r><p>caf\xe9</p></r>",
        TextModel::Lxml,
    )
    .unwrap();
    assert_eq!(doc.root.find_child("p").unwrap().text(), "caf\u{e9}");
    // A value the attribute scan rejects fails upstream too.
    assert!(parse_xml(b"<r a=>", TextModel::Lxml)
        .unwrap_err()
        .to_string()
        .starts_with("parsing a document failed: malformed XML: bad attribute"));
}

#[test]
fn xml_top_level_leftovers() {
    // A top-level comment drops: the root still parses, with no children.
    let doc = parse_xml(b"<!--c--><r/>", TextModel::Lxml).unwrap();
    assert_eq!(doc.root.tag(), "r");
    assert!(doc.root.children.is_empty());
    // Empty character data contributes no text.
    let doc = parse_xml(b"<a><![CDATA[]]></a>", TextModel::Lxml).unwrap();
    assert_eq!(doc.root.text(), "");
    // An unknown entity in text fails upstream too.
    assert!(parse_xml(b"<r>&unknown;</r>", TextModel::Lxml).is_err());
    // Character data past the root is extra content upstream too.
    assert!(parse_xml(b"<a/><![CDATA[x]]>", TextModel::Lxml).is_err());
    // A bare reference past the root is extra content upstream too.
    assert!(parse_xml(b"<a/>&amp;", TextModel::Lxml).is_err());
}

#[test]
fn pdf_unwind_guard_answers_errors() {
    // Malformed content (an undefined font) panics inside the extractor
    // where the module's engine substitutes: the parse answers an error,
    // never a crash. (Known boundary: the module reads 'Hi' here.)
    use lopdf::{dictionary, Document, Object, Stream};
    let mut doc = Document::with_version("1.4");
    let pages_id = doc.new_object_id();
    let font_id = doc.new_object_id();
    let content_id = doc.new_object_id();
    let page_id = doc.new_object_id();
    doc.objects.insert(
        font_id,
        Object::Dictionary(
            dictionary! {"Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Helvetica"},
        ),
    );
    doc.objects.insert(
        content_id,
        Object::Stream(Stream::new(
            dictionary! {},
            b"BT /Nope 12 Tf 72 720 Td (Hi) Tj ET".to_vec(),
        )),
    );
    doc.objects.insert(
        page_id,
        Object::Dictionary(dictionary! {"Type" => "Page", "Parent" => pages_id,
            "MediaBox" => vec![0.into(), 0.into(), 612.into(), 792.into()],
            "Contents" => Object::Reference(content_id),
            "Resources" => Object::Dictionary(dictionary! {"Font" => Object::Dictionary(dictionary! {"F1" => Object::Reference(font_id)})})}),
    );
    doc.objects.insert(
        pages_id,
        Object::Dictionary(dictionary! {"Type" => "Pages", "Kids" => vec![Object::Reference(page_id)], "Count" => 1}),
    );
    let catalog_id = doc.new_object_id();
    doc.objects.insert(
        catalog_id,
        Object::Dictionary(
            dictionary! {"Type" => "Catalog", "Pages" => Object::Reference(pages_id)},
        ),
    );
    doc.trailer.set("Root", Object::Reference(catalog_id));
    let mut raw = Vec::new();
    doc.save_to(&mut raw).unwrap();
    let err = pdf::parse_bytes(&raw, "bad.pdf").unwrap_err();
    assert_eq!(
        err.to_string(),
        "parsing a document failed: PDF text extraction failed"
    );
    let err = pdf::extract_pages(&raw).unwrap_err();
    assert_eq!(
        err.to_string(),
        "parsing a document failed: PDF text extraction failed"
    );
}

#[test]
fn xml_entity_reference_loop_errors() {
    // A cyclic custom entity never settles: upstream reports a reference
    // loop, so this failure stands — and it exercises the text expander's
    // error edge, which unknown names cannot reach (the tokenizer rejects
    // those before expansion ever sees them).
    let err = parse_xml(
        b"<!DOCTYPE r [<!ENTITY a '&b;'><!ENTITY b '&a;'>]><r>&a;</r>",
        TextModel::Lxml,
    )
    .unwrap_err();
    assert_eq!(
        err.to_string(),
        "parsing a document failed: malformed XML: entity expansion does not terminate"
    );
}

#[test]
fn xml_resolved_reference_past_root_errors() {
    // A declared reference past the root expands fine and then has nowhere
    // to go: extra content upstream too. (An undeclared one never reaches
    // the tree push — expansion fails first.)
    assert!(parse_xml(b"<!DOCTYPE r [<!ENTITY q 'Q'>]><r/>&q;", TextModel::Lxml).is_err());
}

#[test]
fn entities_trailing_bare_name_stays_literal() {
    // A name cut off at the input end has no tail to glue with: the head
    // passes through for the later pass, which reads `&amp` the lenient way
    // (`x&`, as the module proves on `<p>x&amp</p>`).
    assert_eq!(
        marginalia_parse::normalize_entities::normalize_entities("&amp").unwrap(),
        "&amp"
    );
}
