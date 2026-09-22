//! Phase 0 acceptance: wire parity with the Pydantic models, validation
//! parity with their validators, and the node-tree behavior from
//! `tests/unit/domain/test_nodes.py`.
//!
//! Each test names its Python source: `domain/*.py`, `sdk/types.py`, or the
//! existing unit test it mirrors.

use chrono::{DateTime, Utc};
use marginalia_types::citations::CitationItemDraft;
use marginalia_types::claims::{
    AnchorDraft, AnchorInput, AnchorRole, ClaimDraft, ClaimEdgeDraft, ClaimKind, ClaimRelation,
    ClaimStatus, CLAIM_AUDIT_ASSURANCE,
};
use marginalia_types::common::{FusionMode, MentionSource, NodeKind};
use marginalia_types::nodes::{
    attach_nodes, build_node_tree, deepest_containing, DocumentNode, DocumentNodeDraft, Section,
    ROOT_PATH,
};
use marginalia_types::provenance::{PluginActivation, PluginActivationState};
use marginalia_types::sdk::{
    availability_rank, Availability, NodeDraft, PassageDraft, SourceMatch, SourceRef,
};
use marginalia_types::works::{
    BlockSourceLinkDraft, Placement, RevisionState, WaiverDraft, WorkBlockDraft, WorkDraft,
    WorkRevisionDraft, WorkStatus,
};
use marginalia_types::works_files::{is_entry_id, CitationEntry, Intent};
use serde_json::{json, Map, Value};
use uuid::Uuid;

fn uid(n: u8) -> Uuid {
    Uuid::parse_str(&format!("11111111-2222-3333-4444-5555555555{n:02x}")).unwrap()
}

fn t() -> DateTime<Utc> {
    "2026-01-15T12:00:00Z".parse().unwrap()
}

// --- Wire values: enums serialize exactly as the StrEnums do ---

#[test]
fn claim_vocab_wire_values() {
    assert_eq!(json!(ClaimKind::Opposition), json!("opposition"));
    assert_eq!(json!(ClaimKind::Mine), json!("mine"));
    assert_eq!(json!(ClaimKind::Premise), json!("premise"));
    assert_eq!(json!(ClaimKind::Lexical), json!("lexical"));
    assert_eq!(json!(ClaimKind::Ally), json!("ally"));
    assert_eq!(json!(ClaimStatus::Researching), json!("researching"));
    assert_eq!(json!(ClaimStatus::Rebutted), json!("rebutted"));
    assert_eq!(json!(ClaimStatus::Weakened), json!("weakened"));
    assert_eq!(json!(ClaimStatus::Unresolved), json!("unresolved"));
    assert_eq!(json!(ClaimStatus::Conceded), json!("conceded"));
    assert_eq!(json!(ClaimRelation::DependsOn), json!("depends_on"));
    assert_eq!(json!(ClaimRelation::Contradicts), json!("contradicts"));
    assert_eq!(json!(ClaimRelation::Refines), json!("refines"));
    assert_eq!(json!(ClaimRelation::Concedes), json!("concedes"));
    assert_eq!(json!(ClaimRelation::Entails), json!("entails"));
    assert_eq!(json!(AnchorRole::Asserts), json!("asserts"));
    assert_eq!(json!(Intent::SeeAlso), json!("see_also"));
    assert_eq!(json!(Placement::BlockEnd), json!("block_end"));
    assert_eq!(json!(RevisionState::Superseded), json!("superseded"));
    assert_eq!(json!(WorkStatus::Archived), json!("archived"));
    assert_eq!(json!(FusionMode::VectorOnly), json!("vector_only"));
    assert_eq!(json!(MentionSource::LlmExtraction), json!("llm_extraction"));
    assert_eq!(json!(NodeKind::Passage), json!("passage"));
    assert_eq!(json!(Availability::ExternalOnly), json!("external_only"));
    assert_eq!(
        json!(PluginActivationState::PendingApproval),
        json!("pending_approval")
    );
}

#[test]
fn availability_rank_matches_python_table() {
    assert_eq!(availability_rank(Availability::InCorpus), 4);
    assert_eq!(availability_rank(Availability::Ingestable), 3);
    assert_eq!(availability_rank(Availability::Borrowable), 2);
    assert_eq!(availability_rank(Availability::Purchasable), 1);
    assert_eq!(availability_rank(Availability::ExternalOnly), 0);
}

#[test]
fn audit_assurance_text_is_byte_identical() {
    assert_eq!(
        CLAIM_AUDIT_ASSURANCE,
        "Green means the implemented mechanical checks found no failure. \
         It does not mean the argument is sound or the source has been interpreted faithfully."
    );
}

// --- Validation parity ---

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
fn node_draft_span_rules() {
    let ok = NodeDraft {
        path: "r.n0".to_owned(),
        parent_path: Some(ROOT_PATH.to_owned()),
        depth: 1,
        position: 0,
        node_type: "section".to_owned(),
        title: None,
        char_start: 0,
        char_end: 10,
        metadata: Map::new(),
    };
    assert!(ok.validate().is_ok());
    let bad = NodeDraft {
        char_start: 10,
        char_end: 4,
        ..ok.clone()
    };
    assert!(bad.validate().unwrap_err().to_string().contains("precedes"));
}

#[test]
fn document_node_draft_span_rules_mirror_test_nodes() {
    let bad = DocumentNodeDraft {
        path: "r.n0".to_owned(),
        parent_path: Some("r".to_owned()),
        depth: 1,
        position: 0,
        node_type: "section".to_owned(),
        title: None,
        char_start: 10,
        char_end: 4,
        metadata: Map::new(),
    };
    assert!(bad.validate().unwrap_err().to_string().contains("precedes"));
}

#[test]
fn citation_item_draft_identity_and_quote_rules() {
    let base = CitationItemDraft {
        occurrence_id: uid(1),
        position: 0,
        edition_id: None,
        edition_key: Some("wlc".to_owned()),
        source_span_id: None,
        quoted_text: None,
        verify_status: None,
        locator: Map::new(),
        prefix: None,
        suffix: None,
        suppress_author: false,
    };
    assert!(base.validate().is_ok());

    let no_edition = CitationItemDraft {
        edition_key: None,
        ..base.clone()
    };
    assert!(no_edition
        .validate()
        .unwrap_err()
        .to_string()
        .contains("names its edition"));

    let bare_quote = CitationItemDraft {
        quoted_text: Some("bereshit".to_owned()),
        ..base
    };
    assert!(bare_quote
        .validate()
        .unwrap_err()
        .to_string()
        .contains("unaddressed"));
}

#[test]
fn citation_entry_handle_and_span_rules() {
    let base = CitationEntry {
        id: "c12".to_owned(),
        intent: Intent::Quotation,
        role: None,
        document_id: uid(2),
        char_start: 0,
        char_end: 5,
        quoted_text: "text".to_owned(),
        edition: None,
        edition_key: None,
        locator: Map::new(),
    };
    assert!(base.validate().is_ok());
    assert!(is_entry_id("c0") && is_entry_id("c123"));
    assert!(!is_entry_id("c") && !is_entry_id("x1") && !is_entry_id("c1a") && !is_entry_id(""));

    for bad in [
        CitationEntry {
            id: "x1".to_owned(),
            ..base.clone()
        },
        CitationEntry {
            char_start: -1,
            ..base.clone()
        },
        CitationEntry {
            quoted_text: "  ".to_owned(),
            ..base.clone()
        },
        CitationEntry {
            char_end: 0,
            ..base.clone()
        },
    ] {
        assert!(bad.validate().is_err(), "{bad:?}");
    }
}

#[test]
fn claim_and_anchor_rules() {
    let claim = ClaimDraft {
        r#ref: "c1".to_owned(),
        statement: "mishpat is restorative".to_owned(),
        kind: ClaimKind::Mine,
        status: ClaimStatus::Open,
        confidence: Some(0.8),
        steelman: None,
        public_ready: false,
        academic_candidate: false,
        attributes: Map::new(),
    };
    assert!(claim.validate().is_ok());
    assert!(ClaimDraft {
        statement: "  ".to_owned(),
        ..claim.clone()
    }
    .validate()
    .is_err());
    assert!(ClaimDraft {
        confidence: Some(2.0),
        ..claim
    }
    .validate()
    .is_err());

    let anchor = AnchorDraft {
        role: AnchorRole::Supports,
        quoted_text: "tsedeq".to_owned(),
        source_span_id: uid(3),
        person_entity_id: None,
        verify_status: marginalia_types::claims::AnchorVerifyStatus::Exact,
        verified_at: t(),
        parser_version: None,
        edition_id: None,
        edition_key: None,
        locator: Map::new(),
    };
    assert!(anchor.validate().is_ok());
    assert!(AnchorDraft {
        role: AnchorRole::Asserts,
        ..anchor
    }
    .validate()
    .unwrap_err()
    .to_string()
    .contains("must name a person"));

    let input = AnchorInput {
        role: AnchorRole::Asserts,
        quote: "q".to_owned(),
        document_id: uid(4),
        person: Some("  ".to_owned()),
        edition_key: None,
        locator: Map::new(),
    };
    assert!(input.validate().is_err());
    assert!(ClaimEdgeDraft {
        target_ref: " ".to_owned(),
        relation: ClaimRelation::Supports,
        confidence: None,
        note: None,
    }
    .validate()
    .is_err());
}

#[test]
fn works_draft_rules() {
    assert!(WorkDraft {
        slug: " ".to_owned(),
        title: "T".to_owned(),
        work_type: "essay".to_owned(),
        language: None,
        abstract_text: None,
        metadata: Map::new(),
    }
    .validate()
    .is_err());
    assert!(WorkRevisionDraft {
        work_id: uid(5),
        revision_number: 0,
        parent_revision_id: None,
        message: None,
        created_by: "user".to_owned(),
        metadata: Map::new(),
    }
    .validate()
    .unwrap_err()
    .to_string()
    .contains("starts at 1"));
    assert!(WorkBlockDraft {
        revision_id: uid(5),
        block_key: uid(6),
        parent_id: None,
        position: -1,
        block_type: "prose".to_owned(),
        title: None,
        body_markdown: String::new(),
        attributes: Map::new(),
    }
    .validate()
    .is_err());
    assert!(BlockSourceLinkDraft {
        block_id: uid(5),
        source_span_id: uid(7),
        relation: "renders".to_owned(),
        confidence: Some(1.5),
        note: None,
    }
    .validate()
    .is_err());
    assert!(WaiverDraft {
        revision_id: uid(5),
        rule_id: "AUTH_X".to_owned(),
        subject: None,
        actor: " ".to_owned(),
        reason: "why".to_owned(),
    }
    .validate()
    .unwrap_err()
    .to_string()
    .contains("who, not just why"));
}

#[test]
fn source_ref_location_rule() {
    let bare = SourceRef {
        path: None,
        uri: None,
        content_hash: None,
        metadata: Map::new(),
    };
    assert!(bare.validate().is_err());
    let uri = SourceRef {
        uri: Some("https://example.test/x".to_owned()),
        ..bare.clone()
    };
    assert!(uri.validate().is_ok());
    assert!(!uri.is_local());
    assert_eq!(uri.r#ref(), "https://example.test/x");
    let local = SourceRef {
        path: Some("/tmp/x.md".into()),
        ..bare
    };
    assert!(local.validate().is_ok() && local.is_local());
}

#[test]
fn confidence_bounds_on_scored_dtos() {
    assert!(SourceMatch {
        plugin: "p".to_owned(),
        source_id: "s".to_owned(),
        title: "t".to_owned(),
        authors: vec![],
        year: None,
        doi: None,
        isbn: None,
        availability: Availability::ExternalOnly,
        confidence: 1.2,
        ingest_action: None,
        document_id: None,
        metadata: Map::new(),
    }
    .validate()
    .is_err());
}

#[test]
fn plugin_activation_identity_rule() {
    let legacy: PluginActivation =
        serde_json::from_value(json!({"plugin_id": "p", "distribution_version": "1",
            "manifest": {}, "permissions_granted": {}, "installed_at": "2026-01-01T00:00:00Z"}))
        .unwrap();
    assert!(legacy.validate().is_ok());

    let mut enabled = legacy.clone();
    enabled.state = PluginActivationState::Enabled;
    let err = enabled.validate().unwrap_err().to_string();
    assert!(err.contains("distribution_name"), "{err}");
    assert!(err.contains("approved_at"), "{err}");
}

// --- Node-tree behavior (mirrors tests/unit/domain/test_nodes.py) ---

fn section(start: i64, end: i64, heading: Option<&str>, level: Option<i64>) -> Section {
    Section {
        char_start: Some(start),
        char_end: Some(end),
        level,
        heading: heading.map(str::to_owned),
        node_type: None,
        extra: Map::new(),
    }
}

#[test]
fn empty_document_still_has_a_root() {
    let drafts = build_node_tree(&[], 500, Some("Empty")).unwrap();
    assert_eq!(drafts.len(), 1);
    assert_eq!(drafts[0].path, ROOT_PATH);
    assert_eq!((drafts[0].char_start, drafts[0].char_end), (0, 500));
}

#[test]
fn flat_sections_parent_to_root() {
    let drafts = build_node_tree(
        &[
            section(0, 10, Some("One"), Some(1)),
            section(12, 20, Some("Two"), Some(1)),
        ],
        20,
        None,
    )
    .unwrap();
    assert_eq!(
        drafts.iter().map(|d| d.path.as_str()).collect::<Vec<_>>(),
        [ROOT_PATH, "r.n0", "r.n1"]
    );
    assert!(drafts[1..]
        .iter()
        .all(|d| d.parent_path.as_deref() == Some(ROOT_PATH)));
    assert_eq!(
        drafts.iter().map(|d| d.depth).collect::<Vec<_>>(),
        [0, 1, 1]
    );
    assert_eq!(drafts[1].position, 0);
    assert_eq!(drafts[2].position, 1);
}

#[test]
fn levels_nest_and_skipped_levels_find_shallower_parent() {
    let drafts = build_node_tree(
        &[
            section(0, 10, Some("Chapter"), Some(1)),
            section(10, 20, Some("Section"), Some(2)),
            section(20, 30, Some("Subsection"), Some(3)),
            section(30, 40, Some("Next chapter"), Some(1)),
        ],
        40,
        None,
    )
    .unwrap();
    let by_title: Map<String, Value> = Map::new();
    let _ = by_title;
    let find = |t: &str| {
        drafts
            .iter()
            .find(|d| d.title.as_deref() == Some(t))
            .unwrap()
    };
    assert_eq!(
        find("Section").parent_path,
        Some(find("Chapter").path.clone())
    );
    assert_eq!(
        find("Subsection").parent_path,
        Some(find("Section").path.clone())
    );
    assert_eq!(find("Next chapter").parent_path.as_deref(), Some(ROOT_PATH));
    assert_eq!(find("Subsection").depth, 3);

    let drafts = build_node_tree(
        &[
            section(0, 10, Some("Chapter"), Some(1)),
            section(10, 20, Some("Deep"), Some(3)),
        ],
        20,
        None,
    )
    .unwrap();
    let find = |t: &str| {
        drafts
            .iter()
            .find(|d| d.title.as_deref() == Some(t))
            .unwrap()
    };
    assert_eq!(find("Deep").parent_path, Some(find("Chapter").path.clone()));
}

#[test]
fn parents_widen_to_contain_children() {
    let drafts = build_node_tree(
        &[
            section(0, 10, Some("Chapter"), Some(1)),
            section(10, 25, Some("Section"), Some(2)),
            section(25, 60, Some("Deeper"), Some(3)),
        ],
        60,
        None,
    )
    .unwrap();
    let chapter = drafts
        .iter()
        .find(|d| d.title.as_deref() == Some("Chapter"))
        .unwrap();
    assert_eq!((chapter.char_start, chapter.char_end), (0, 60));

    let by_path: std::collections::HashMap<&str, &DocumentNodeDraft> =
        drafts.iter().map(|d| (d.path.as_str(), d)).collect();
    for draft in &drafts {
        if let Some(parent) = draft
            .parent_path
            .as_ref()
            .and_then(|p| by_path.get(p.as_str()))
        {
            assert!(parent.char_start <= draft.char_start);
            assert!(draft.char_end <= parent.char_end);
            assert!(by_path.contains_key(draft.path.as_str()));
        }
    }
    // Parents precede children so inserts can resolve ids in order.
    let mut seen = std::collections::HashSet::new();
    for draft in &drafts {
        if let Some(parent) = &draft.parent_path {
            assert!(seen.contains(parent));
        }
        seen.insert(draft.path.clone());
    }
}

#[test]
fn spanless_sections_are_skipped_and_extras_become_metadata() {
    let spanless = Section {
        char_start: None,
        char_end: None,
        level: Some(1),
        heading: Some("Spanless".to_owned()),
        node_type: None,
        extra: Map::new(),
    };
    let drafts =
        build_node_tree(&[section(0, 10, Some("Real"), Some(1)), spanless], 10, None).unwrap();
    assert_eq!(
        drafts.iter().map(|d| d.title.clone()).collect::<Vec<_>>(),
        [None, Some("Real".to_owned())]
    );

    let mut extra = Map::new();
    extra.insert("href".to_owned(), Value::String("c1.xhtml".to_owned()));
    let drafts = build_node_tree(
        &[Section {
            char_start: Some(0),
            char_end: Some(5),
            level: Some(1),
            heading: Some("H".to_owned()),
            node_type: None,
            extra,
        }],
        5,
        None,
    )
    .unwrap();
    assert_eq!(
        drafts[1].metadata.get("href"),
        Some(&Value::String("c1.xhtml".to_owned()))
    );
}

fn node(id: Uuid, depth: i64, start: i64, end: i64) -> DocumentNode {
    DocumentNode {
        id,
        document_id: uid(9),
        parent_id: None,
        path: "p".to_owned(),
        depth,
        position: 0,
        node_type: "section".to_owned(),
        title: None,
        char_start: start,
        char_end: end,
        metadata: Map::new(),
        created_at: t(),
    }
}

#[test]
fn deepest_containing_prefers_innermost_and_straddles_resolve_up() {
    let (root, inner, sibling) = (
        node(uid(10), 0, 0, 100),
        node(uid(11), 1, 10, 50),
        node(uid(12), 1, 50, 90),
    );
    let nodes = [root.clone(), inner.clone(), sibling.clone()];
    assert_eq!(deepest_containing(&nodes, 20, 30).unwrap().id, inner.id);
    assert_eq!(deepest_containing(&nodes, 30, 60).unwrap().id, root.id);
    assert!(deepest_containing(&[node(uid(13), 1, 0, 10)], 50, 60).is_none());
}

#[test]
fn attach_nodes_stamps_containers_and_passes_through_empty() {
    let (root, inner) = (node(uid(10), 0, 0, 100), node(uid(11), 1, 10, 50));
    let nodes = [root.clone(), inner.clone()];
    let mut d1 = passage_draft();
    d1.char_start = 10;
    d1.char_end = 20;
    d1.text = "x".repeat(10);
    let mut d2 = passage_draft();
    d2.char_start = 60;
    d2.char_end = 70;
    d2.text = "y".repeat(10);
    let stamped = attach_nodes(vec![d1, d2], &nodes);
    assert_eq!(stamped[0].node_id, Some(inner.id));
    assert_eq!(stamped[1].node_id, Some(root.id));

    let drafts = vec![passage_draft()];
    let passthrough = attach_nodes(drafts.clone(), &[]);
    assert_eq!(passthrough, drafts);
}

// --- Serde shape: defaults, renames, forbid-extra ---

#[test]
fn search_query_defaults_match_python() {
    let q: marginalia_types::passages::SearchQuery =
        serde_json::from_value(json!({"text": "mishpat"})).unwrap();
    assert_eq!((q.k, q.k_vec, q.k_kw), (20, 100, 100));
    assert_eq!(q.fusion_mode, FusionMode::Rrf);
    assert!((q.alpha - 0.5).abs() < f64::EPSILON);
    assert!(q.rerank && q.rerank_n == 30);
    let f: marginalia_types::passages::SearchFilters = serde_json::from_value(json!({})).unwrap();
    assert_eq!(
        f.extension_logic,
        marginalia_types::passages::ExtensionLogic::And
    );
}

#[test]
fn python_wire_names_survive_round_trip() {
    // `abstract` and `schema` are reserved-adjacent renames, not renames-away.
    let work: Value = serde_json::from_value(json!({
        "id": uid(20).to_string(), "slug": "s", "title": "t", "work_type": "essay",
        "abstract": "a", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"
    }))
    .unwrap();
    let _ = work;
    let parsed: marginalia_types::works::Work = serde_json::from_value(json!({
        "id": uid(20).to_string(), "slug": "s", "title": "t", "work_type": "essay",
        "abstract": "a", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"
    }))
    .unwrap();
    assert_eq!(parsed.abstract_text.as_deref(), Some("a"));
    assert_eq!(
        serde_json::to_value(&parsed).unwrap().get("abstract"),
        Some(&Value::String("a".to_owned()))
    );

    let schema: marginalia_types::extractions::ExtractionSchema = serde_json::from_value(json!({
        "id": uid(21).to_string(), "name": "n", "version": 1, "owner": "core",
        "schema": {"type": "object"}, "prompt_template": "p",
        "created_at": "2026-01-01T00:00:00Z"
    }))
    .unwrap();
    assert_eq!(
        serde_json::to_value(&schema).unwrap().get("schema"),
        Some(&json!({"type": "object"}))
    );
}

#[test]
fn forbid_extra_rejects_unknown_keys_where_python_does() {
    let err = serde_json::from_value::<CitationItemDraft>(json!({
        "occurrence_id": uid(1).to_string(), "bogus": 1
    }))
    .unwrap_err()
    .to_string();
    assert!(err.contains("bogus"), "{err}");
    // Non-forbid models ignore unknown keys, as Pydantic does by default.
    let draft: DocumentNodeDraft = serde_json::from_value(json!({
        "path": "r.n0", "parent_path": "r", "depth": 1, "position": 0,
        "char_start": 0, "char_end": 5, "unknown": "kept-out"
    }))
    .unwrap();
    assert_eq!(draft.path, "r.n0");
}

#[test]
fn bytes_and_datetimes_round_trip() {
    // Bytes cross JSON as UTF-8 strings, exactly like Pydantic's JSON mode.
    let mut doc = marginalia_types::documents::Document {
        id: uid(30),
        title: None,
        document_type: "generic".to_owned(),
        language: None,
        source: "s".to_owned(),
        content_hash: b"abc123".to_vec(),
        parser: "p".to_owned(),
        parser_version: "1".to_owned(),
        ingested_at: t(),
        created_date_start: None,
        created_date_end: None,
        created_precision: None,
        edition_id: None,
        metadata: Map::new(),
    };
    let v = serde_json::to_value(&doc).unwrap();
    assert_eq!(v.get("content_hash"), Some(&json!("abc123")));
    assert_eq!(v.get("ingested_at"), Some(&json!("2026-01-15T12:00:00Z")));
    let back: marginalia_types::documents::Document = serde_json::from_value(v).unwrap();
    assert_eq!(back, doc);
    // Non-UTF-8 payloads fail loudly instead of silently crossing —
    // Pydantic raises PydanticSerializationError for the same input.
    doc.content_hash = vec![0, 1, 255];
    assert!(serde_json::to_value(&doc).is_err());
}
#[test]
fn edition_id_round_trips_and_defaults_to_none() {
    // Mirrors the `edition_id` cases in `test_domain_types.py`: unset by
    // default, kept when set, and old payloads without the key still parse.
    let mut doc = marginalia_types::documents::Document {
        id: uid(31),
        title: None,
        document_type: "generic".to_owned(),
        language: None,
        source: "s".to_owned(),
        content_hash: b"abc123".to_vec(),
        parser: "p".to_owned(),
        parser_version: "1".to_owned(),
        ingested_at: t(),
        created_date_start: None,
        created_date_end: None,
        created_precision: None,
        edition_id: None,
        metadata: Map::new(),
    };
    assert_eq!(doc.edition_id, None);
    let edition = uid(32);
    doc.edition_id = Some(edition);
    let v = serde_json::to_value(&doc).unwrap();
    assert_eq!(v.get("edition_id"), Some(&json!(edition.to_string())));
    // Key order mirrors the Python model: edition before metadata.
    let keys: Vec<&str> = v.as_object().unwrap().keys().map(String::as_str).collect();
    let pos = keys.iter().position(|k| *k == "edition_id").unwrap();
    assert_eq!(keys[pos + 1], "metadata");
    let back: marginalia_types::documents::Document = serde_json::from_value(v).unwrap();
    assert_eq!(back, doc);
    let mut old = serde_json::to_value(&doc).unwrap();
    old.as_object_mut().unwrap().remove("edition_id");
    let back: marginalia_types::documents::Document = serde_json::from_value(old).unwrap();
    assert_eq!(back.edition_id, None);
}

#[test]
fn plugin_context_carries_a_database_url_it_never_prints() {
    // Mirrors `test_boundary_dtos_validate_consumer_visible_fields`: the URL
    // is held and comparable, but `repr`/`Debug` never exposes it.
    use marginalia_types::sdk::PluginContext;
    let bare = PluginContext {
        plugin_id: "sample".to_owned(),
        data_dir: std::path::PathBuf::from("/tmp/x"),
        distribution_name: "marginalia-ai-plugin-sample".to_owned(),
        distribution_version: "1.2.3".to_owned(),
        database_url: None,
    };
    assert_eq!(bare.database_url, None);
    let secret = "postgresql+asyncpg://user:secret@db/research";
    let loaded = PluginContext {
        database_url: Some(secret.to_owned()),
        ..bare.clone()
    };
    assert_eq!(loaded.database_url.as_deref(), Some(secret));
    let rendered = format!("{loaded:?}");
    assert!(!rendered.contains("secret"), "{rendered}");
    assert!(rendered.contains("**********"), "{rendered}");
    let v = serde_json::to_value(&loaded).unwrap();
    assert_eq!(v.get("database_url"), Some(&json!(secret)));
    let back: PluginContext = serde_json::from_value(v).unwrap();
    assert_eq!(back, loaded);
    let mut old = serde_json::to_value(&bare).unwrap();
    old.as_object_mut().unwrap().remove("database_url");
    let back: PluginContext = serde_json::from_value(old).unwrap();
    assert_eq!(back.database_url, None);
}

#[test]
fn error_display_never_empty() {
    for e in [
        marginalia_types::Error::StaleWrite,
        marginalia_types::Error::WorksNotConfigured,
        marginalia_types::Error::NotFound {
            kind: "document",
            id: uid(40).to_string(),
        },
    ] {
        assert!(!marginalia_types::errors::describe(&e).is_empty());
    }
}

#[test]
fn validators_accept_valid_models() {
    // Every `validate()` ok-path, so error branches are not the only ones run.
    let claim = marginalia_types::claims::ClaimDraft {
        r#ref: "c1".to_owned(),
        statement: "s".to_owned(),
        kind: marginalia_types::claims::ClaimKind::Premise,
        status: marginalia_types::claims::ClaimStatus::Open,
        confidence: Some(0.5),
        steelman: None,
        public_ready: false,
        academic_candidate: false,
        attributes: Map::new(),
    };
    assert!(claim.validate().is_ok());
    assert!(marginalia_types::claims::ClaimEdgeDraft {
        target_ref: "c2".to_owned(),
        relation: marginalia_types::claims::ClaimRelation::Supports,
        confidence: Some(1.0),
        note: None,
    }
    .validate()
    .is_ok());
    assert!(marginalia_types::claims::AnchorDraft {
        role: marginalia_types::claims::AnchorRole::Asserts,
        quoted_text: "q".to_owned(),
        source_span_id: uid(1),
        person_entity_id: Some(uid(2)),
        verify_status: marginalia_types::claims::AnchorVerifyStatus::Near,
        verified_at: t(),
        parser_version: None,
        edition_id: None,
        edition_key: None,
        locator: Map::new(),
    }
    .validate()
    .is_ok());
    assert!(marginalia_types::claims::AnchorInput {
        role: marginalia_types::claims::AnchorRole::Asserts,
        quote: "q".to_owned(),
        document_id: uid(1),
        person: Some("Amos".to_owned()),
        edition_key: None,
        locator: Map::new(),
    }
    .validate()
    .is_ok());
    assert!(marginalia_types::works::WorkDraft {
        slug: "s".to_owned(),
        title: "t".to_owned(),
        work_type: "essay".to_owned(),
        language: None,
        abstract_text: None,
        metadata: Map::new(),
    }
    .validate()
    .is_ok());
    assert!(marginalia_types::works::WorkRevisionDraft {
        work_id: uid(1),
        revision_number: 2,
        parent_revision_id: None,
        message: None,
        created_by: "user".to_owned(),
        metadata: Map::new(),
    }
    .validate()
    .is_ok());
    assert!(marginalia_types::works::WorkBlockDraft {
        revision_id: uid(1),
        block_key: uid(2),
        parent_id: None,
        position: 0,
        block_type: "prose".to_owned(),
        title: None,
        body_markdown: String::new(),
        attributes: Map::new(),
    }
    .validate()
    .is_ok());
    for confidence in [None, Some(0.0), Some(1.0)] {
        assert!(marginalia_types::works::BlockSourceLinkDraft {
            block_id: uid(1),
            source_span_id: uid(2),
            relation: "r".to_owned(),
            confidence,
            note: None,
        }
        .validate()
        .is_ok());
    }
    assert!(marginalia_types::works::WaiverDraft {
        revision_id: uid(1),
        rule_id: "R".to_owned(),
        subject: None,
        actor: "me".to_owned(),
        reason: "why".to_owned(),
    }
    .validate()
    .is_ok());
    assert!(marginalia_types::sdk::SourceMatch {
        plugin: "p".to_owned(),
        source_id: "s".to_owned(),
        title: "t".to_owned(),
        authors: vec![],
        year: None,
        doi: None,
        isbn: None,
        availability: marginalia_types::sdk::Availability::InCorpus,
        confidence: 0.0,
        ingest_action: None,
        document_id: None,
        metadata: Map::new(),
    }
    .validate()
    .is_ok());
}

#[test]
fn sdk_drafts_validate_all_branches() {
    let mut draft = marginalia_types::sdk::PassageDraft {
        position: 0,
        char_start: 0,
        char_end: 2,
        locator: Map::new(),
        text: "hi".to_owned(),
        token_count: Some(1),
        chunker: "c".to_owned(),
        chunker_version: "1".to_owned(),
        metadata: Map::new(),
        node_id: None,
    };
    assert!(draft.validate().is_ok());
    draft.position = -1;
    assert!(draft.validate().is_err());
    draft.position = 0;
    draft.token_count = Some(0);
    assert!(draft.validate().is_ok());

    let mut node = marginalia_types::sdk::NodeDraft {
        path: "r".to_owned(),
        parent_path: None,
        depth: 0,
        position: 0,
        node_type: "section".to_owned(),
        title: None,
        char_start: 0,
        char_end: 1,
        metadata: Map::new(),
    };
    assert!(node.validate().is_ok());
    node.depth = -1;
    assert!(node.validate().is_err());
    node.depth = 0;
    node.char_end = -1;
    assert!(node.validate().is_err());

    assert!(marginalia_types::sdk::DetectionResult {
        confidence: 0.0,
        reason: "r".to_owned(),
        is_viable: true,
    }
    .validate()
    .is_ok());
    assert!(marginalia_types::sdk::DetectionResult {
        confidence: 1.1,
        reason: "r".to_owned(),
        is_viable: false,
    }
    .validate()
    .is_err());
}

#[test]
fn sdk_wire_defaults_match_python() {
    let parsed: marginalia_types::sdk::ParsedDocument =
        serde_json::from_value(json!({"text": "hi"})).unwrap();
    assert_eq!(parsed.document_type, "generic");
    assert!(parsed.sections.is_empty() && parsed.structural_locators.is_empty());

    let q: marginalia_types::sdk::SdkSearchQuery =
        serde_json::from_value(json!({"text": "x", "filters": {}})).unwrap();
    assert_eq!((q.k, q.k_vec, q.k_kw, q.rerank_n), (20, 100, 100, 30));
    assert!(q.rerank && (q.alpha - 0.5).abs() < f64::EPSILON);
    assert_eq!(q.fusion_mode, marginalia_types::common::FusionMode::Rrf);
    assert_eq!(
        q.filters.unwrap().extension_logic,
        marginalia_types::sdk::ExtensionLogic::And
    );

    let event: marginalia_types::sdk::SdkEvent = serde_json::from_value(json!({
        "id": uid(1).to_string(), "event_type": "e", "created_at": "2026-01-01T00:00:00Z"
    }))
    .unwrap();
    assert!((event.confidence - 1.0).abs() < f64::EPSILON);
    let edge: marginalia_types::sdk::SdkEdge = serde_json::from_value(json!({
        "id": uid(1).to_string(), "source_kind": "passage", "source_id": uid(1).to_string(),
        "target_kind": "entity", "target_id": uid(2).to_string(), "relation_type": "r",
        "created_at": "2026-01-01T00:00:00Z"
    }))
    .unwrap();
    assert!((edge.confidence - 1.0).abs() < f64::EPSILON);

    let node: marginalia_types::sdk::NodeDraft = serde_json::from_value(json!({
        "path": "r.n0", "char_end": 5
    }))
    .unwrap();
    assert_eq!(
        (
            node.node_type.as_str(),
            node.depth,
            node.position,
            node.char_start
        ),
        ("section", 0, 0, 0)
    );
    assert!(node.validate().is_ok());

    let draft: marginalia_types::sdk::PassageDraft = serde_json::from_value(json!({
        "char_start": 0, "char_end": 2, "text": "hi",
        "chunker": "c", "chunker_version": "1"
    }))
    .unwrap();
    assert_eq!(draft.position, 0);
    assert!(draft.validate().is_ok());

    let detected: marginalia_types::sdk::DetectionResult =
        serde_json::from_value(json!({"confidence": 0.5, "reason": "r"})).unwrap();
    assert!(detected.is_viable);

    let matched: marginalia_types::sdk::SourceMatch =
        serde_json::from_value(json!({"plugin": "p", "source_id": "s", "title": "t"})).unwrap();
    assert_eq!(
        matched.availability,
        marginalia_types::sdk::Availability::ExternalOnly
    );
    assert!(matched.authors.is_empty());
}

#[test]
fn core_draft_defaults_match_python() {
    let draft: marginalia_types::documents::DocumentDraft = serde_json::from_value(json!({
        "source": "s", "content_hash": "abc", "parser": "p", "parser_version": "1"
    }))
    .unwrap();
    assert_eq!(draft.document_type, "generic");

    let edge: marginalia_types::edges::EdgeDraft = serde_json::from_value(json!({
        "source_kind": "passage", "source_id": uid(1).to_string(),
        "target_kind": "entity", "target_id": uid(2).to_string(), "relation_type": "r"
    }))
    .unwrap();
    assert!((edge.confidence - 1.0).abs() < f64::EPSILON);

    let event: marginalia_types::events::EventDraft =
        serde_json::from_value(json!({"event_type": "e"})).unwrap();
    assert!((event.confidence - 1.0).abs() < f64::EPSILON);

    let revision: marginalia_types::works::WorkRevisionDraft = serde_json::from_value(json!({
        "work_id": uid(1).to_string()
    }))
    .unwrap();
    assert_eq!(
        (revision.revision_number, revision.created_by.as_str()),
        (1, "user")
    );
    assert!(revision.validate().is_ok());

    let waiver: marginalia_types::works::WaiverDraft = serde_json::from_value(json!({
        "revision_id": uid(1).to_string(), "rule_id": "R", "reason": "why"
    }))
    .unwrap();
    assert_eq!(waiver.actor.as_str(), "user");
    assert!(waiver.validate().is_ok());

    let opts: marginalia_types::extractions::ExtractionOptions =
        serde_json::from_value(json!({})).unwrap();
    assert_eq!(
        (opts.concurrency, opts.batch_size, opts.caller.as_str()),
        (8, 10, "core")
    );
    assert!(opts.retry_on_validation_error && !opts.force_refresh);
}

#[test]
fn extraction_from_cached_copies_fields() {
    let extraction = marginalia_types::extractions::Extraction {
        id: uid(1),
        passage_id: uid(2),
        schema_id: uid(3),
        extractor_version: "1".to_owned(),
        llm_model: "m".to_owned(),
        status: marginalia_types::common::ExtractionStatus::Ok,
        error: None,
        records: vec![Map::new()],
        llm_call_id: Some(uid(4)),
        created_at: t(),
    };
    let result = marginalia_types::extractions::ExtractionResult::from_cached(&extraction);
    assert_eq!(result.passage_id, uid(2));
    assert!(result.from_cache && result.error.is_none());
    assert_eq!(result.llm_call_id, Some(uid(4)));
    assert_eq!(result.records.len(), 1);
}

#[test]
fn provenance_helpers() {
    let full: marginalia_types::provenance::PluginActivation = serde_json::from_value(json!({
        "plugin_id": "p", "distribution_name": "d", "distribution_version": "1",
        "entry_point_name": "e", "manifest_sha256": "m", "manifest": {},
        "permissions_granted": {}, "installed_at": "2026-01-01T00:00:00Z",
        "state": "enabled", "approved_at": "2026-01-02T00:00:00Z"
    }))
    .unwrap();
    assert!(full.validate().is_ok());

    let budget = marginalia_types::provenance::BudgetExceeded {
        spent: 12.5,
        limit: 10.0,
        window_days: 30,
    };
    let message = budget.message();
    assert!(message.contains("12.50") && message.contains("30d") && message.contains("10.00"));

    let local = marginalia_types::sdk::SourceRef {
        path: Some("/tmp/x.md".into()),
        uri: None,
        content_hash: None,
        metadata: Map::new(),
    };
    assert_eq!(local.r#ref(), "/tmp/x.md");
}

#[test]
fn describe_falls_back_on_empty_messages() {
    assert_eq!(
        marginalia_types::errors::describe(format_args!("")),
        "unknown error"
    );
    assert_eq!(
        marginalia_types::errors::describe(format_args!("boom")),
        "boom"
    );
}

#[test]
fn optional_hash_wire_forms() {
    // Some UTF-8: string on the wire, like Pydantic.
    let revision = marginalia_types::works::WorkRevision {
        id: uid(1),
        work_id: uid(2),
        revision_number: 1,
        parent_revision_id: None,
        state: marginalia_types::works::RevisionState::Draft,
        message: None,
        content_hash: Some(b"abc".to_vec()),
        created_by: "user".to_owned(),
        created_at: t(),
        frozen_at: None,
        published_at: None,
        metadata: Map::new(),
    };
    let v = serde_json::to_value(&revision).unwrap();
    assert_eq!(v.get("content_hash"), Some(&json!("abc")));
    let back: marginalia_types::works::WorkRevision = serde_json::from_value(v).unwrap();
    assert_eq!(back.content_hash, Some(b"abc".to_vec()));

    // None round-trips as null.
    let mut none = revision.clone();
    none.content_hash = None;
    let v = serde_json::to_value(&none).unwrap();
    assert_eq!(v.get("content_hash"), Some(&Value::Null));
    let back: marginalia_types::works::WorkRevision = serde_json::from_value(v).unwrap();
    assert_eq!(back.content_hash, None);

    // Non-UTF-8 fails loudly, exactly like Pydantic's serialization error.
    let mut bad = revision.clone();
    bad.content_hash = Some(vec![0, 255]);
    assert!(serde_json::to_value(&bad).is_err());
}

#[test]
fn node_draft_guards_and_level_fallback() {
    let bad = marginalia_types::nodes::DocumentNodeDraft {
        path: "r.n0".to_owned(),
        parent_path: Some("r".to_owned()),
        depth: 1,
        position: 0,
        node_type: "section".to_owned(),
        title: None,
        char_start: -2,
        char_end: 5,
        metadata: Map::new(),
    };
    assert!(bad
        .validate()
        .unwrap_err()
        .to_string()
        .contains("non-negative"));

    // A section without a level nests at 1, like Python's `or 1`.
    let drafts = marginalia_types::nodes::build_node_tree(
        &[marginalia_types::nodes::Section {
            char_start: Some(0),
            char_end: Some(5),
            level: None,
            heading: Some("H".to_owned()),
            node_type: None,
            extra: Map::new(),
        }],
        5,
        None,
    )
    .unwrap();
    assert_eq!(drafts[1].depth, 1);
}

#[test]
fn confidence_none_skips_the_bound() {
    assert!(marginalia_types::claims::ClaimDraft {
        r#ref: "c".to_owned(),
        statement: "s".to_owned(),
        kind: marginalia_types::claims::ClaimKind::Ally,
        status: marginalia_types::claims::ClaimStatus::Open,
        confidence: None,
        steelman: None,
        public_ready: false,
        academic_candidate: false,
        attributes: Map::new(),
    }
    .validate()
    .is_ok());
    assert!(marginalia_types::claims::ClaimEdgeDraft {
        target_ref: "c".to_owned(),
        relation: marginalia_types::claims::ClaimRelation::Rebuts,
        confidence: None,
        note: None,
    }
    .validate()
    .is_ok());
}

#[test]
fn empty_quotes_are_refused() {
    assert!(marginalia_types::claims::AnchorDraft {
        role: marginalia_types::claims::AnchorRole::Supports,
        quoted_text: "  ".to_owned(),
        source_span_id: uid(1),
        person_entity_id: None,
        verify_status: marginalia_types::claims::AnchorVerifyStatus::Near,
        verified_at: t(),
        parser_version: None,
        edition_id: None,
        edition_key: None,
        locator: Map::new(),
    }
    .validate()
    .is_err());
    assert!(marginalia_types::claims::AnchorInput {
        role: marginalia_types::claims::AnchorRole::Context,
        quote: String::new(),
        document_id: uid(1),
        person: None,
        edition_key: None,
        locator: Map::new(),
    }
    .validate()
    .is_err());
    assert!(marginalia_types::works::WaiverDraft {
        revision_id: uid(1),
        rule_id: "  ".to_owned(),
        subject: None,
        actor: "me".to_owned(),
        reason: "why".to_owned(),
    }
    .validate()
    .is_err());
}

#[test]
fn edge_confidence_bound_is_checked() {
    assert!(marginalia_types::claims::ClaimEdgeDraft {
        target_ref: "c".to_owned(),
        relation: marginalia_types::claims::ClaimRelation::Entails,
        confidence: Some(2.0),
        note: None,
    }
    .validate()
    .is_err());
}

#[test]
fn node_tree_rejects_unaddressable_sections() {
    let err = marginalia_types::nodes::build_node_tree(
        &[marginalia_types::nodes::Section {
            char_start: Some(-5),
            char_end: Some(5),
            level: Some(1),
            heading: Some("H".to_owned()),
            node_type: None,
            extra: Map::new(),
        }],
        5,
        None,
    )
    .unwrap_err();
    assert!(err.to_string().contains("non-negative"));
}

// --- Works ports: verify-tier vocabulary for the Phase 4 seams ---

#[test]
fn verify_tier_wire_values() {
    use marginalia_types::works_ports::{
        VerifyDivergence, VerifyLocation, VerifyResult, VerifyTier,
    };
    assert_eq!(VerifyTier::Exact.as_str(), "exact");
    assert_eq!(VerifyTier::Normalized.as_str(), "normalized");
    assert_eq!(VerifyTier::Near.as_str(), "near");
    assert_eq!(VerifyTier::NotFound.as_str(), "not_found");
    assert_eq!(VerifyTier::NoCanonicalText.as_str(), "no_canonical_text");
    assert_eq!(
        json!(VerifyTier::NoCanonicalText),
        json!("no_canonical_text")
    );
    let result = VerifyResult {
        tier: VerifyTier::Near,
        location: Some(VerifyLocation {
            document_id: uid(1),
            char_start: 10,
            char_end: 60,
        }),
        matched_fraction: Some(0.5),
        divergence: Some(VerifyDivergence {
            matched_characters: 4,
            matched_tail: "tail".to_owned(),
            quote_continues: "q".to_owned(),
            source_continues: "s".to_owned(),
        }),
    };
    let back: VerifyResult = serde_json::from_value(json!(result)).unwrap();
    assert_eq!(back, result);
}
