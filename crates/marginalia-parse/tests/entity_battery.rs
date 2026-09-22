//! Entity differential vs BeautifulSoup (`html.parser`).
//!
//! The equality battery proves byte-identical decoding (legacy and
//! numeric references, unknowns, clamps, CJK mix). The poison cases
//! pin the documented residual: `&#[0-9]+[a-f]` without a semicolon
//! corrupts html.parser's tag state (following markup leaks as
//! text, version-fragile); the crate decodes per the
//! missing-semicolon rule instead, so documents parse cleanly.
use marginalia_parse::html::parse_text;

#[test]
fn entity_battery_matches_bs() {
    assert_eq!(parse_text("<p>A&amp;B</p>", "e.html").unwrap().text, "A&B");
    assert_eq!(
        parse_text("<p>&#65;&#x42;</p>", "e.html").unwrap().text,
        "AB"
    );
    assert_eq!(
        parse_text("<p>&#38 end</p>", "e.html").unwrap().text,
        "& end"
    );
    assert_eq!(
        parse_text("<p>&bogus; &amp &lt &gt &quot &copy tail</p>", "e.html")
            .unwrap()
            .text,
        "&bogus & < > \" © tail"
    );
    assert_eq!(
        parse_text("<p>&#; &#x &# &;</p>", "e.html").unwrap().text,
        "&#; &#x &# &;"
    );
    assert_eq!(
        parse_text("<p>&#0; &#xD800; &#x110000; &#999999999999;</p>", "e.html")
            .unwrap()
            .text,
        "� � � �"
    );
    assert_eq!(
        parse_text("<p>&lt= &AMP; &#38 &#x26</p>", "e.html")
            .unwrap()
            .text,
        "<= & & &"
    );
    assert_eq!(
        parse_text("<p>caf&eacute; na&iuml;ve &frac12;</p>", "e.html")
            .unwrap()
            .text,
        "café naïve ½"
    );
    assert_eq!(
        parse_text("<p>é&#233; 😀&#x1F600;</p>", "e.html")
            .unwrap()
            .text,
        "éé 😀😀"
    );
    assert_eq!(
        parse_text("<p>end &#38 b</p><p>after</p>", "e.html")
            .unwrap()
            .text,
        "end & b\nafter"
    );
    assert_eq!(
        parse_text("<p>&#38g &#38; tail</p>", "e.html")
            .unwrap()
            .text,
        "&g & tail"
    );
}

#[test]
fn poison_inputs_parse_cleanly() {
    assert_eq!(
        parse_text("<p>A&#38b C</p>", "e.html").unwrap().text,
        "A&b C"
    );
    assert_eq!(
        parse_text("<p>X&#65D Y</p>", "e.html").unwrap().text,
        "XAD Y"
    );
    assert_eq!(
        parse_text("<p>x&#12aby</p>", "e.html").unwrap().text,
        "x\x0caby"
    );
}
