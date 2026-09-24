//! Kernel acceptance: the SDK DTOs the shipped seams produce keep
//! `research_engine_sdk`'s validation rules and wire defaults.

use marginalia_types::sdk::{ParsedDocument, PassageDraft};
use serde_json::{json, Map};

fn passage_draft() -> PassageDraft {
    PassageDraft {
        position: 0,
        char_start: 0,
        char_end: 5,
        locator: Map::new(),
        text: "hello".to_owned(),
        token_count: None,
        chunker: "fixed_window".to_owned(),
        chunker_version: "1".to_owned(),
        metadata: Map::new(),
        node_id: None,
    }
}

#[test]
fn passage_draft_span_rules() {
    assert!(passage_draft().validate().is_ok());
    // Unicode width counts scalar values, like Python's len(str).
    let mut d = passage_draft();
    d.text = "héllo".to_owned();
    assert!(d.validate().is_ok());

    let mut d = passage_draft();
    d.char_start = -1;
    assert!(d.validate().is_err());

    let mut d = passage_draft();
    (d.char_start, d.char_end) = (4, 2);
    let err = d.validate().unwrap_err().to_string();
    assert!(err.contains("precedes"), "{err}");

    let mut d = passage_draft();
    d.text = "hi".to_owned();
    let err = d.validate().unwrap_err().to_string();
    assert!(err.contains("disagree"), "{err}");

    let mut d = passage_draft();
    d.token_count = Some(-1);
    assert!(d.validate().is_err());
}

#[test]
fn passage_draft_position_and_token_edges() {
    let mut draft = passage_draft();
    draft.position = -1;
    assert!(draft.validate().is_err());
    draft.position = 0;
    draft.token_count = Some(0);
    assert!(draft.validate().is_ok());
}

#[test]
fn sdk_wire_defaults_match_python() {
    let parsed: ParsedDocument = serde_json::from_value(json!({"text": "hi"})).unwrap();
    assert_eq!(parsed.document_type, "generic");
    assert!(parsed.sections.is_empty() && parsed.structural_locators.is_empty());

    let draft: PassageDraft = serde_json::from_value(json!({
        "char_start": 0, "char_end": 2, "text": "hi",
        "chunker": "c", "chunker_version": "1"
    }))
    .unwrap();
    assert_eq!(draft.position, 0);
    assert!(draft.validate().is_ok());
}
