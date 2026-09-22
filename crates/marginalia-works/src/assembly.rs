//! Assemble one revision's tree with its citations, links, and spans.
//!
//! Python source: `services/works/assembly.py`. `work_get`, validation,
//! freezing, export, and trace all walk the same shape: blocks depth-first,
//! each with its occurrences (and their items) and its links, plus every
//! span the items and source links name.

use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use marginalia_types::citations::BlockCitations;
use marginalia_types::ports::{CitationRepo, SourceSpanRepo, WorkBlockRepo, WorkLinkRepo};
use marginalia_types::spans::SourceSpan;
use marginalia_types::works::Placement;
use marginalia_types::works::{BlockLinks, Work, WorkBlock, WorkRevision};
use marginalia_types::works_files::Intent;

use crate::hashing::compute_content_hash;

/// One block with everything hung on it, and its parent's key.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AssembledBlock {
    pub block: WorkBlock,
    pub parent_key: Option<Uuid>,
    #[serde(default)]
    pub citations: Vec<BlockCitations>,
    #[serde(default)]
    pub links: Option<BlockLinks>,
}

/// A revision ready to validate, hash, export, or trace.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AssembledRevision {
    pub work: Work,
    pub revision: WorkRevision,
    #[serde(default)]
    pub blocks: Vec<AssembledBlock>,
    /// Every span named by an item or a source link, by id.
    #[serde(default)]
    pub spans: HashMap<Uuid, SourceSpan>,
}

impl AssembledRevision {
    /// Blocks by row-id string, for occurrence and link joins.
    #[must_use]
    pub fn block_index(&self) -> HashMap<String, &AssembledBlock> {
        self.blocks
            .iter()
            .map(|item| (item.block.id.to_string(), item))
            .collect()
    }

    /// Blocks by block-key string, for marker and parent joins.
    #[must_use]
    pub fn key_index(&self) -> HashMap<String, &AssembledBlock> {
        self.blocks
            .iter()
            .map(|item| (item.block.block_key.to_string(), item))
            .collect()
    }
}

/// The `Intent.value` string Python interpolates into the hash rows.
fn intent_value(intent: Intent) -> &'static str {
    match intent {
        Intent::Quotation => "quotation",
        Intent::Translation => "translation",
        Intent::Support => "support",
        Intent::Contrast => "contrast",
        Intent::Background => "background",
        Intent::Definition => "definition",
        Intent::Source => "source",
        Intent::SeeAlso => "see_also",
    }
}

/// The `Placement.value` string Python interpolates into the hash rows.
fn placement_value(placement: Placement) -> &'static str {
    match placement {
        Placement::Inline => "inline",
        Placement::BlockEnd => "block_end",
    }
}

fn opt_string(value: &Option<String>) -> Value {
    match value {
        Some(text) => Value::String(text.clone()),
        None => Value::Null,
    }
}

fn opt_uuid(value: &Option<Uuid>) -> Value {
    match value {
        Some(id) => Value::String(id.to_string()),
        None => Value::Null,
    }
}

/// The frozen-revision content hash: authored content, nothing volatile.
///
/// Blocks in tree order; citations and links sorted by identity. Row ids
/// turn over on copy-forward and timestamps move on reverify, so neither
/// enters — `AUTH_REVISION_MUTATED` means the words changed.
pub fn hash_assembled(view: &AssembledRevision) -> [u8; 32] {
    let blocks: Vec<Map<String, Value>> = view
        .blocks
        .iter()
        .map(|item| {
            let mut row = Map::new();
            row.insert(
                "block_key".to_owned(),
                Value::String(item.block.block_key.to_string()),
            );
            row.insert("parent_key".to_owned(), opt_uuid(&item.parent_key));
            row.insert("position".to_owned(), Value::from(item.block.position));
            row.insert(
                "block_type".to_owned(),
                Value::String(item.block.block_type.clone()),
            );
            row.insert("title".to_owned(), opt_string(&item.block.title));
            row.insert(
                "body_markdown".to_owned(),
                Value::String(item.block.body_markdown.clone()),
            );
            row
        })
        .collect();
    let citations: Vec<Map<String, Value>> = view
        .blocks
        .iter()
        .flat_map(|item| {
            item.citations.iter().flat_map(move |entry| {
                entry.items.iter().map(move |row| {
                    let mut projected = Map::new();
                    projected.insert(
                        "block_key".to_owned(),
                        Value::String(item.block.block_key.to_string()),
                    );
                    projected.insert(
                        "citation_key".to_owned(),
                        Value::String(entry.occurrence.citation_key.to_string()),
                    );
                    projected.insert("position".to_owned(), Value::from(row.position));
                    projected.insert(
                        "intent".to_owned(),
                        Value::String(intent_value(entry.occurrence.intent).to_owned()),
                    );
                    projected.insert(
                        "placement".to_owned(),
                        Value::String(placement_value(entry.occurrence.placement).to_owned()),
                    );
                    projected.insert("edition_id".to_owned(), opt_uuid(&row.edition_id));
                    projected.insert("edition_key".to_owned(), opt_string(&row.edition_key));
                    projected.insert("source_span_id".to_owned(), opt_uuid(&row.source_span_id));
                    projected.insert("quoted_text".to_owned(), opt_string(&row.quoted_text));
                    projected.insert("verify_status".to_owned(), opt_string(&row.verify_status));
                    projected.insert("locator".to_owned(), Value::Object(row.locator.clone()));
                    projected.insert("prefix".to_owned(), opt_string(&row.prefix));
                    projected.insert("suffix".to_owned(), opt_string(&row.suffix));
                    projected.insert(
                        "suppress_author".to_owned(),
                        Value::Bool(row.suppress_author),
                    );
                    projected
                })
            })
        })
        .collect();
    let source_links: Vec<Map<String, Value>> = view
        .blocks
        .iter()
        .flat_map(|item| {
            let sources = item
                .links
                .as_ref()
                .map_or(&[][..], |links| &links.sources[..]);
            sources.iter().map(move |link| {
                let mut projected = Map::new();
                projected.insert(
                    "block_key".to_owned(),
                    Value::String(item.block.block_key.to_string()),
                );
                projected.insert(
                    "source_span_id".to_owned(),
                    Value::String(link.source_span_id.to_string()),
                );
                projected.insert("relation".to_owned(), Value::String(link.relation.clone()));
                projected.insert(
                    "confidence".to_owned(),
                    match link.confidence {
                        Some(confidence) => serde_json::Number::from_f64(confidence)
                            .map_or(Value::Null, Value::Number),
                        None => Value::Null,
                    },
                );
                projected.insert("note".to_owned(), opt_string(&link.note));
                projected
            })
        })
        .collect();
    let entity_links: Vec<Map<String, Value>> = view
        .blocks
        .iter()
        .flat_map(|item| {
            let entities = item
                .links
                .as_ref()
                .map_or(&[][..], |links| &links.entities[..]);
            entities.iter().map(move |link| {
                let mut projected = Map::new();
                projected.insert(
                    "block_key".to_owned(),
                    Value::String(item.block.block_key.to_string()),
                );
                projected.insert(
                    "entity_id".to_owned(),
                    Value::String(link.entity_id.to_string()),
                );
                projected.insert("relation".to_owned(), Value::String(link.relation.clone()));
                projected.insert("surface_form".to_owned(), opt_string(&link.surface_form));
                projected
            })
        })
        .collect();
    compute_content_hash(&blocks, &citations, &source_links, &entity_links)
}

/// Load the tree, attach citations and links, and cache the spans.
pub async fn assemble_revision<B, C, L, S>(
    work: &Work,
    revision: &WorkRevision,
    blocks: &B,
    citations: &C,
    links: &L,
    spans: &S,
) -> marginalia_types::Result<AssembledRevision>
where
    B: WorkBlockRepo,
    C: CitationRepo,
    L: WorkLinkRepo,
    S: SourceSpanRepo,
{
    let tree = blocks.tree(revision.id).await?;
    let by_id: HashMap<Uuid, &WorkBlock> = tree.iter().map(|block| (block.id, block)).collect();
    let attached = citations.for_revision(revision.id).await?;
    let mut per_block: HashMap<String, Vec<BlockCitations>> = HashMap::new();
    for entry in attached {
        per_block
            .entry(entry.occurrence.block_id.to_string())
            .or_default()
            .push(entry);
    }

    let mut assembled: Vec<AssembledBlock> = Vec::with_capacity(tree.len());
    let mut span_ids: HashSet<Uuid> = HashSet::new();
    // The link rows are needed for the span walk below, so fetch them into a
    // side table first; the assembled rows are built in tree order after.
    let mut link_table: HashMap<Uuid, BlockLinks> = HashMap::with_capacity(tree.len());
    for block in &tree {
        let block_links = links.for_block(block.id).await?;
        for link in &block_links.sources {
            span_ids.insert(link.source_span_id);
        }
        link_table.insert(block.id, block_links);
    }
    for block in &tree {
        let parent_key = match block.parent_id {
            Some(parent_id) => by_id.get(&parent_id).map(|parent| parent.block_key),
            None => None,
        };
        let block_citations = per_block
            .get(&block.id.to_string())
            .cloned()
            .unwrap_or_default();
        for entry in &block_citations {
            for item in &entry.items {
                if let Some(span_id) = item.source_span_id {
                    span_ids.insert(span_id);
                }
            }
        }
        // proof: every tree row got a link row in the loop above.
        let block_links = link_table
            .remove(&block.id)
            .expect("link table holds one row per tree block");
        assembled.push(AssembledBlock {
            block: block.clone(),
            parent_key,
            citations: block_citations,
            links: Some(block_links),
        });
    }

    let mut span_index: HashMap<Uuid, SourceSpan> = HashMap::new();
    for span_id in span_ids {
        if let Some(span) = spans.get(span_id).await? {
            span_index.insert(span_id, span);
        }
    }
    Ok(AssembledRevision {
        work: work.clone(),
        revision: revision.clone(),
        blocks: assembled,
        spans: span_index,
    })
}

#[cfg(test)]
mod tests {
    use std::future::Future;
    use std::pin::Pin;
    use std::task::{Context, Poll, RawWaker, RawWakerVTable, Waker};

    /// Minimal executor: the fakes never pend, so a spin poll drives the
    /// future to readiness. (`tokio` is not a dependency of this crate,
    /// and `Cargo.toml` is owned by another slice.)
    fn block_on<F: Future>(mut future: F) -> F::Output {
        fn raw() -> RawWaker {
            fn noop(_: *const ()) {}
            fn clone(ptr: *const ()) -> RawWaker {
                raw_waker(ptr)
            }
            fn raw_waker(ptr: *const ()) -> RawWaker {
                RawWaker::new(ptr, &RawWakerVTable::new(clone, noop, noop, noop))
            }
            raw_waker(std::ptr::null())
        }
        // SAFETY: the waker never dereferences its null data pointer; the
        // futures polled here never clone, wake, or drop through it.
        let waker = unsafe { Waker::from_raw(raw()) };
        let mut context = Context::from_waker(&waker);
        // SAFETY: `future` is stack-owned and never moved after pinning.
        let mut pinned = unsafe { Pin::new_unchecked(&mut future) };
        loop {
            match pinned.as_mut().poll(&mut context) {
                Poll::Ready(value) => return value,
                Poll::Pending => std::thread::yield_now(),
            }
        }
    }

    use super::*;
    use std::sync::Mutex;

    use chrono::Utc;
    use marginalia_types::citations::{CitationItem, CitationOccurrence};
    use marginalia_types::ports::{CitationRepo, SourceSpanRepo, WorkBlockRepo, WorkLinkRepo};
    use marginalia_types::works::{BlockEntityLink, BlockSourceLink, WorkStatus};
    use marginalia_types::{Error, Result};

    const KEY_A: &str = "11111111-1111-1111-1111-111111111111";
    const KEY_B: &str = "22222222-2222-2222-2222-222222222222";
    const CITE_A: &str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";

    fn work() -> Work {
        let now = Utc::now();
        Work {
            id: Uuid::parse_str("33333333-3333-3333-3333-333333333333").expect("fixture id"),
            slug: "deror".to_owned(),
            title: "Deror".to_owned(),
            work_type: "translation".to_owned(),
            status: WorkStatus::Draft,
            language: None,
            abstract_text: None,
            current_revision_id: None,
            metadata: Map::new(),
            created_at: now,
            updated_at: now,
            archived_at: None,
        }
    }

    fn revision(work_id: Uuid) -> WorkRevision {
        let now = Utc::now();
        WorkRevision {
            id: Uuid::parse_str("44444444-4444-4444-4444-444444444444").expect("fixture id"),
            work_id,
            revision_number: 1,
            parent_revision_id: None,
            state: marginalia_types::works::RevisionState::Draft,
            message: None,
            content_hash: None,
            created_by: "user".to_owned(),
            created_at: now,
            frozen_at: None,
            published_at: None,
            metadata: Map::new(),
        }
    }

    fn block(id: &str, key: &str, parent_id: Option<Uuid>, body: &str) -> WorkBlock {
        let now = Utc::now();
        WorkBlock {
            id: Uuid::parse_str(id).expect("fixture id"),
            revision_id: Uuid::parse_str("44444444-4444-4444-4444-444444444444")
                .expect("fixture id"),
            block_key: Uuid::parse_str(key).expect("fixture key"),
            parent_id,
            position: 0,
            block_type: "paragraph".to_owned(),
            title: None,
            body_markdown: body.to_owned(),
            attributes: Map::new(),
            created_at: now,
            updated_at: now,
        }
    }

    fn view(body: &str, row_id: &str) -> AssembledRevision {
        let work = work();
        AssembledRevision {
            revision: revision(work.id),
            work,
            blocks: vec![AssembledBlock {
                block: block(row_id, KEY_A, None, body),
                parent_key: None,
                citations: Vec::new(),
                links: Some(BlockLinks::default()),
            }],
            spans: HashMap::new(),
        }
    }

    #[test]
    fn test_row_ids_and_timestamps_do_not_move_the_hash() {
        assert_eq!(
            hash_assembled(&view("words", "55555555-5555-5555-5555-555555555555")),
            hash_assembled(&view("words", "66666666-6666-6666-6666-666666666666"))
        );
    }

    #[test]
    fn test_words_move_the_hash() {
        assert_ne!(
            hash_assembled(&view("words", "55555555-5555-5555-5555-555555555555")),
            hash_assembled(&view("other words", "55555555-5555-5555-5555-555555555555"))
        );
    }

    #[test]
    fn test_none_projections_hash_stably() {
        // Mirrors the Python `_view_with_citation` shape: a spanless item with
        // no edition id and an entity link with no surface form must project
        // as nulls, not missing keys, and hash the same twice.
        let now = Utc::now();
        let occurrence = CitationOccurrence {
            id: Uuid::parse_str("77777777-7777-7777-7777-777777777777").expect("fixture"),
            citation_key: Uuid::parse_str(CITE_A).expect("fixture"),
            block_id: Uuid::parse_str("55555555-5555-5555-5555-555555555555").expect("fixture"),
            placement: Placement::BlockEnd,
            intent: Intent::Background,
            note: None,
            created_at: now,
        };
        let item = CitationItem {
            occurrence_id: occurrence.id,
            position: 0,
            edition_id: None,
            edition_key: Some("DABAR_2026".to_owned()),
            source_span_id: None,
            quoted_text: None,
            verify_status: None,
            verified_at: None,
            locator: Map::new(),
            prefix: None,
            suffix: None,
            suppress_author: false,
        };
        let entity = BlockEntityLink {
            block_id: Uuid::parse_str("55555555-5555-5555-5555-555555555555").expect("fixture"),
            entity_id: Uuid::parse_str("99999999-9999-9999-9999-999999999999").expect("fixture"),
            relation: "renders".to_owned(),
            surface_form: None,
            created_at: now,
        };
        let paragraph = block(
            "55555555-5555-5555-5555-555555555555",
            KEY_B,
            None,
            "A claim.",
        );
        let w = work();
        let assembled = AssembledRevision {
            revision: revision(w.id),
            work: w,
            blocks: vec![AssembledBlock {
                block: paragraph,
                parent_key: Uuid::parse_str(KEY_A).ok(),
                citations: vec![BlockCitations {
                    occurrence,
                    items: vec![item],
                }],
                links: Some(BlockLinks {
                    sources: Vec::new(),
                    entities: vec![entity],
                }),
            }],
            spans: HashMap::new(),
        };
        assert_eq!(hash_assembled(&assembled), hash_assembled(&assembled));
    }

    #[test]
    fn test_indexes_key_blocks_by_row_id_and_block_key() {
        let assembled = view("words", "55555555-5555-5555-5555-555555555555");
        let by_id = assembled.block_index();
        let by_key = assembled.key_index();
        assert!(by_id.contains_key("55555555-5555-5555-5555-555555555555"));
        assert!(by_key.contains_key(KEY_A));
        assert_eq!(
            by_id["55555555-5555-5555-5555-555555555555"]
                .block
                .body_markdown,
            "words"
        );
    }

    struct FakeBlocks {
        tree: Vec<WorkBlock>,
        fail_tree: bool,
    }

    struct FakeCitations {
        attached: Vec<BlockCitations>,
        fail_list: bool,
    }

    struct FakeLinks {
        sources: Vec<BlockSourceLink>,
        fail_block: bool,
    }

    struct FakeSpans {
        spans: Mutex<HashMap<Uuid, SourceSpan>>,
        fail_get: bool,
    }

    impl WorkBlockRepo for FakeBlocks {
        type Tx = ();
        async fn upsert(
            &self,
            _tx: &mut Self::Tx,
            _revision_id: Uuid,
            _draft: marginalia_types::works::WorkBlockDraft,
            _expected_updated_at: Option<chrono::DateTime<Utc>>,
        ) -> Result<WorkBlock> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn tree(&self, _revision_id: Uuid) -> Result<Vec<WorkBlock>> {
            if self.fail_tree {
                return Err(Error::Storage("tree failed".to_owned()));
            }
            Ok(self.tree.clone())
        }
        async fn get(&self, _block_id: Uuid) -> Result<Option<WorkBlock>> {
            Ok(None)
        }
        async fn by_key(&self, _revision_id: Uuid, _block_key: Uuid) -> Result<Option<WorkBlock>> {
            Ok(None)
        }
        async fn by_key_in_tx(
            &self,
            _tx: &mut Self::Tx,
            _revision_id: Uuid,
            _block_key: Uuid,
        ) -> Result<Option<WorkBlock>> {
            Ok(None)
        }
        async fn delete(&self, _tx: &mut Self::Tx, _block_id: Uuid) -> Result<()> {
            Ok(())
        }
    }

    impl CitationRepo for FakeCitations {
        type Tx = ();
        async fn insert_occurrence(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::citations::OccurrenceDraft,
        ) -> Result<CitationOccurrence> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn insert_item(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::citations::CitationItemDraft,
        ) -> Result<CitationItem> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn for_block(&self, _block_id: Uuid) -> Result<Vec<BlockCitations>> {
            Ok(Vec::new())
        }
        async fn for_revision(&self, _revision_id: Uuid) -> Result<Vec<BlockCitations>> {
            if self.fail_list {
                return Err(Error::Storage("citations failed".to_owned()));
            }
            Ok(self.attached.clone())
        }
        async fn by_key(
            &self,
            _revision_id: Uuid,
            _citation_key: Uuid,
        ) -> Result<Option<BlockCitations>> {
            Ok(None)
        }
        async fn citing_span(&self, _span_id: Uuid) -> Result<Vec<BlockCitations>> {
            Ok(Vec::new())
        }
        async fn citing_key(&self, _edition_key: &str) -> Result<Vec<BlockCitations>> {
            Ok(Vec::new())
        }
    }

    impl WorkLinkRepo for FakeLinks {
        type Tx = ();
        async fn add_source_link(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::works::BlockSourceLinkDraft,
        ) -> Result<BlockSourceLink> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn add_entity_link(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::works::BlockEntityLinkDraft,
        ) -> Result<BlockEntityLink> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn for_block(&self, _block_id: Uuid) -> Result<BlockLinks> {
            if self.fail_block {
                return Err(Error::Storage("links failed".to_owned()));
            }
            Ok(BlockLinks {
                sources: self.sources.clone(),
                entities: Vec::new(),
            })
        }
        async fn for_span(&self, _span_id: Uuid) -> Result<Vec<BlockSourceLink>> {
            Ok(Vec::new())
        }
        async fn for_entity(
            &self,
            _entity_id: Uuid,
            _relation: &str,
        ) -> Result<Vec<BlockEntityLink>> {
            Ok(Vec::new())
        }
    }

    impl SourceSpanRepo for FakeSpans {
        type Tx = ();
        async fn resolve(
            &self,
            _tx: &mut Self::Tx,
            _document_id: Uuid,
            _char_start: i64,
            _char_end: i64,
        ) -> Result<SourceSpan> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn get(&self, span_id: Uuid) -> Result<Option<SourceSpan>> {
            if self.fail_get {
                return Err(Error::Storage("span failed".to_owned()));
            }
            Ok(self.spans.lock().expect("span lock").get(&span_id).cloned())
        }
        async fn for_document(&self, _document_id: Uuid) -> Result<Vec<SourceSpan>> {
            Ok(Vec::new())
        }
        async fn stale(&self, _limit: i64) -> Result<Vec<SourceSpan>> {
            Ok(Vec::new())
        }
    }

    #[test]
    fn test_assemble_joins_tree_citations_and_spans() {
        let root_id = Uuid::parse_str("55555555-5555-5555-5555-555555555555").expect("fixture");
        let child_id = Uuid::parse_str("66666666-6666-6666-6666-666666666666").expect("fixture");
        let mut root = block("55555555-5555-5555-5555-555555555555", KEY_A, None, "Root.");
        root.position = 0;
        let mut child = block(
            "66666666-6666-6666-6666-666666666666",
            KEY_B,
            Some(root_id),
            "Child.",
        );
        child.position = 1;
        let now = Utc::now();
        let occurrence = CitationOccurrence {
            id: Uuid::new_v4(),
            citation_key: Uuid::parse_str(CITE_A).expect("fixture"),
            block_id: child_id,
            placement: Placement::Inline,
            intent: Intent::Quotation,
            note: None,
            created_at: now,
        };
        let span_id = Uuid::new_v4();
        let item = CitationItem {
            occurrence_id: occurrence.id,
            position: 0,
            edition_id: Some(Uuid::new_v4()),
            edition_key: Some("DABAR_2026".to_owned()),
            source_span_id: Some(span_id),
            quoted_text: Some("Child.".to_owned()),
            verify_status: Some("exact".to_owned()),
            verified_at: None,
            locator: Map::new(),
            prefix: None,
            suffix: None,
            suppress_author: false,
        };
        let attached = vec![BlockCitations {
            occurrence,
            items: vec![item],
        }];
        let document_id = Uuid::new_v4();
        let span = SourceSpan {
            id: span_id,
            document_id,
            char_start: 0,
            char_end: 6,
            quoted_text: "Child.".to_owned(),
            parser: None,
            parser_version: None,
            passage_id: None,
            created_at: now,
        };
        let w = work();
        let rev = revision(w.id);
        let assembled = block_on(assemble_revision(
            &w,
            &rev,
            &FakeBlocks {
                tree: vec![root, child],
                fail_tree: false,
            },
            &FakeCitations {
                attached,
                fail_list: false,
            },
            &FakeLinks {
                sources: Vec::new(),
                fail_block: false,
            },
            &FakeSpans {
                spans: Mutex::new(HashMap::from([(span_id, span)])),
                fail_get: false,
            },
        ))
        .expect("assemble");
        assert_eq!(assembled.blocks.len(), 2);
        assert_eq!(assembled.blocks[0].parent_key, None);
        assert_eq!(
            assembled.blocks[1].parent_key,
            Some(Uuid::parse_str(KEY_A).expect("fixture"))
        );
        assert!(assembled.blocks[0].citations.is_empty());
        assert_eq!(assembled.blocks[1].citations.len(), 1);
        assert!(assembled.spans.contains_key(&span_id));
        let _ = root_id;
    }
    #[test]
    fn test_intent_and_placement_values_match_python() {
        // The hash rows interpolate `Intent.value` / `Placement.value`, so
        // the mapping is contract, not trivia.
        assert_eq!(intent_value(Intent::Quotation), "quotation");
        assert_eq!(intent_value(Intent::Translation), "translation");
        assert_eq!(intent_value(Intent::Support), "support");
        assert_eq!(intent_value(Intent::Contrast), "contrast");
        assert_eq!(intent_value(Intent::Background), "background");
        assert_eq!(intent_value(Intent::Definition), "definition");
        assert_eq!(intent_value(Intent::Source), "source");
        assert_eq!(intent_value(Intent::SeeAlso), "see_also");
        assert_eq!(placement_value(Placement::Inline), "inline");
        assert_eq!(placement_value(Placement::BlockEnd), "block_end");
    }

    #[test]
    fn test_assemble_hashes_source_links_and_skips_missing_spans() {
        // Source links join the span cache like items do; a span id with no
        // cached row names nothing, exactly as Python's `Span` join skips it.
        let now = Utc::now();
        let root = block("55555555-5555-5555-5555-555555555555", KEY_A, None, "Root.");
        let cached_id = Uuid::new_v4();
        let missing_id = Uuid::new_v4();
        let sources = vec![
            BlockSourceLink {
                block_id: root.id,
                source_span_id: cached_id,
                relation: "discusses".to_owned(),
                confidence: Some(0.9),
                note: Some("central".to_owned()),
                created_at: now,
            },
            BlockSourceLink {
                block_id: root.id,
                source_span_id: missing_id,
                relation: "mentions".to_owned(),
                confidence: None,
                note: None,
                created_at: now,
            },
        ];
        let cached = SourceSpan {
            id: cached_id,
            document_id: Uuid::new_v4(),
            char_start: 0,
            char_end: 4,
            quoted_text: "Root".to_owned(),
            parser: None,
            parser_version: None,
            passage_id: None,
            created_at: now,
        };
        let w = work();
        let rev = revision(w.id);
        let assembled = block_on(assemble_revision(
            &w,
            &rev,
            &FakeBlocks {
                tree: vec![root],
                fail_tree: false,
            },
            &FakeCitations {
                attached: Vec::new(),
                fail_list: false,
            },
            &FakeLinks {
                sources: sources.clone(),
                fail_block: false,
            },
            &FakeSpans {
                spans: Mutex::new(HashMap::from([(cached_id, cached)])),
                fail_get: false,
            },
        ))
        .expect("assemble");
        assert!(assembled.spans.contains_key(&cached_id));
        assert!(!assembled.spans.contains_key(&missing_id));
        // The links move the hash: confidence, note, and relation are all
        // authored content, and the hash is stable across runs.
        let bare = block_on(assemble_revision(
            &w,
            &rev,
            &FakeBlocks {
                tree: vec![block(
                    "55555555-5555-5555-5555-555555555555",
                    KEY_A,
                    None,
                    "Root.",
                )],
                fail_tree: false,
            },
            &FakeCitations {
                attached: Vec::new(),
                fail_list: false,
            },
            &FakeLinks {
                sources: Vec::new(),
                fail_block: false,
            },
            &FakeSpans {
                spans: Mutex::new(HashMap::new()),
                fail_get: false,
            },
        ))
        .expect("assemble");
        assert_ne!(hash_assembled(&assembled), hash_assembled(&bare));
        assert_eq!(hash_assembled(&assembled), hash_assembled(&assembled));
    }

    #[test]
    fn test_assemble_skips_spanless_citation_items() {
        // A citation item naming no span contributes nothing to the span
        // cache: the `None` arm skips it while the citation itself still
        // attaches to its block.
        let now = Utc::now();
        let root = block("55555555-5555-5555-5555-555555555555", KEY_A, None, "Root.");
        let occurrence = CitationOccurrence {
            id: Uuid::new_v4(),
            citation_key: Uuid::parse_str(CITE_A).expect("fixture"),
            block_id: root.id,
            placement: Placement::BlockEnd,
            intent: Intent::Background,
            note: None,
            created_at: now,
        };
        let item = CitationItem {
            occurrence_id: occurrence.id,
            position: 0,
            edition_id: None,
            edition_key: Some("DABAR_2026".to_owned()),
            source_span_id: None,
            quoted_text: None,
            verify_status: None,
            verified_at: None,
            locator: Map::new(),
            prefix: None,
            suffix: None,
            suppress_author: false,
        };
        let w = work();
        let rev = revision(w.id);
        let assembled = block_on(assemble_revision(
            &w,
            &rev,
            &FakeBlocks {
                tree: vec![root],
                fail_tree: false,
            },
            &FakeCitations {
                attached: vec![BlockCitations {
                    occurrence,
                    items: vec![item],
                }],
                fail_list: false,
            },
            &FakeLinks {
                sources: Vec::new(),
                fail_block: false,
            },
            &FakeSpans {
                spans: Mutex::new(HashMap::new()),
                fail_get: false,
            },
        ))
        .expect("assemble");
        assert_eq!(assembled.blocks[0].citations.len(), 1);
        assert!(assembled.spans.is_empty());
    }

    #[test]
    fn test_block_on_drives_a_pending_future_to_readiness() {
        // The spin executor only resolves Pending by yielding: one Pending
        // round-trip plus a waker clone exercises both poll arms.
        struct PendOnce {
            polled: bool,
        }
        impl Future for PendOnce {
            type Output = u32;
            fn poll(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<u32> {
                // Proof: the executor's waker is inert (null data, noop
                // vtable), so cloning it here only exercises the vtable.
                let _ = context.waker().clone();
                if self.polled {
                    Poll::Ready(7)
                } else {
                    self.polled = true;
                    Poll::Pending
                }
            }
        }
        assert_eq!(block_on(PendOnce { polled: false }), 7);
    }

    #[test]
    fn test_fake_ports_surface() {
        // Every fake repo method answers once: unused writers refuse with
        // the sentinel, readers echo the rigged state.
        use marginalia_types::citations::{CitationItemDraft, OccurrenceDraft};
        use marginalia_types::works::{BlockEntityLinkDraft, BlockSourceLinkDraft, WorkBlockDraft};
        let now = Utc::now();
        let root = block("55555555-5555-5555-5555-555555555555", KEY_A, None, "Root.");
        let blocks = FakeBlocks {
            tree: vec![root.clone()],
            fail_tree: false,
        };
        let citations = FakeCitations {
            fail_list: false,
            attached: vec![BlockCitations {
                occurrence: CitationOccurrence {
                    id: Uuid::new_v4(),
                    citation_key: Uuid::parse_str(CITE_A).expect("fixture"),
                    block_id: root.id,
                    placement: Placement::Inline,
                    intent: Intent::Quotation,
                    note: None,
                    created_at: now,
                },
                items: Vec::new(),
            }],
        };
        let links = FakeLinks {
            fail_block: false,
            sources: vec![BlockSourceLink {
                block_id: root.id,
                source_span_id: Uuid::new_v4(),
                relation: "discusses".to_owned(),
                confidence: None,
                note: None,
                created_at: now,
            }],
        };
        let span_id = Uuid::new_v4();
        let spans = FakeSpans {
            fail_get: false,
            spans: Mutex::new(HashMap::from([(
                span_id,
                SourceSpan {
                    id: span_id,
                    document_id: Uuid::new_v4(),
                    char_start: 0,
                    char_end: 4,
                    quoted_text: "Root".to_owned(),
                    parser: None,
                    parser_version: None,
                    passage_id: None,
                    created_at: now,
                },
            )])),
        };
        let mut tx = ();
        let draft = WorkBlockDraft {
            revision_id: Uuid::new_v4(),
            block_key: Uuid::new_v4(),
            parent_id: None,
            position: 0,
            block_type: "paragraph".to_owned(),
            title: None,
            body_markdown: "Root.".to_owned(),
            attributes: Map::new(),
        };
        {
            let error = block_on(blocks.upsert(&mut tx, draft.revision_id, draft, None))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert_eq!(
            block_on(blocks.tree(Uuid::new_v4())).expect("tree").len(),
            1
        );
        assert!(block_on(blocks.get(Uuid::new_v4())).expect("get").is_none());
        assert!(block_on(blocks.by_key(Uuid::new_v4(), Uuid::new_v4()))
            .expect("by_key")
            .is_none());
        assert!(
            block_on(blocks.by_key_in_tx(&mut tx, Uuid::new_v4(), Uuid::new_v4()))
                .expect("by_key_in_tx")
                .is_none()
        );
        block_on(blocks.delete(&mut tx, Uuid::new_v4())).expect("delete");
        {
            let error = block_on(citations.insert_occurrence(
                &mut tx,
                OccurrenceDraft {
                    block_id: root.id,
                    citation_key: Uuid::new_v4(),
                    placement: Placement::Inline,
                    intent: Intent::Quotation,
                    note: None,
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(citations.insert_item(
                &mut tx,
                CitationItemDraft {
                    occurrence_id: Uuid::new_v4(),
                    position: 0,
                    edition_id: None,
                    edition_key: Some("DABAR_2026".to_owned()),
                    source_span_id: None,
                    quoted_text: None,
                    verify_status: None,
                    locator: Map::new(),
                    prefix: None,
                    suffix: None,
                    suppress_author: false,
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(citations.for_block(root.id))
            .expect("for_block")
            .is_empty());
        assert_eq!(
            block_on(citations.for_revision(Uuid::new_v4()))
                .expect("for_revision")
                .len(),
            1
        );
        assert!(block_on(citations.by_key(Uuid::new_v4(), Uuid::new_v4()))
            .expect("by_key")
            .is_none());
        assert!(block_on(citations.citing_span(span_id))
            .expect("citing_span")
            .is_empty());
        assert!(block_on(citations.citing_key("DABAR_2026"))
            .expect("citing_key")
            .is_empty());
        {
            let error = block_on(links.add_source_link(
                &mut tx,
                BlockSourceLinkDraft {
                    block_id: root.id,
                    source_span_id: Uuid::new_v4(),
                    relation: "discusses".to_owned(),
                    confidence: None,
                    note: None,
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(links.add_entity_link(
                &mut tx,
                BlockEntityLinkDraft {
                    block_id: root.id,
                    entity_id: Uuid::new_v4(),
                    relation: "renders".to_owned(),
                    surface_form: None,
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert_eq!(
            block_on(links.for_block(root.id))
                .expect("for_block")
                .sources
                .len(),
            1
        );
        assert!(block_on(links.for_span(span_id))
            .expect("for_span")
            .is_empty());
        assert!(block_on(links.for_entity(Uuid::new_v4(), "renders"))
            .expect("for_entity")
            .is_empty());
        let mut span_tx = ();
        {
            let error = block_on(spans.resolve(&mut span_tx, Uuid::new_v4(), 0, 4))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert_eq!(
            block_on(spans.get(span_id)).expect("get").expect("span").id,
            span_id
        );
        assert!(block_on(spans.for_document(Uuid::new_v4()))
            .expect("for_document")
            .is_empty());
        assert!(block_on(spans.stale(10)).expect("stale").is_empty());
    }

    #[test]
    fn test_tree_failure_aborts_the_assembly() {
        // Infrastructure errors propagate: a failing tree read surfaces
        // without an assembled view, mirroring the storage-error paths.
        let w = work();
        let rev = revision(w.id);
        let error = block_on(assemble_revision(
            &w,
            &rev,
            &FakeBlocks {
                tree: Vec::new(),
                fail_tree: true,
            },
            &FakeCitations {
                attached: Vec::new(),
                fail_list: false,
            },
            &FakeLinks {
                sources: Vec::new(),
                fail_block: false,
            },
            &FakeSpans {
                spans: Mutex::new(HashMap::new()),
                fail_get: false,
            },
        ))
        .expect_err("tree fails");
        assert!(matches!(error, Error::Storage(message) if message == "tree failed"));
    }

    #[test]
    fn test_citation_failure_aborts_the_assembly() {
        let w = work();
        let rev = revision(w.id);
        let error = block_on(assemble_revision(
            &w,
            &rev,
            &FakeBlocks {
                tree: Vec::new(),
                fail_tree: false,
            },
            &FakeCitations {
                attached: Vec::new(),
                fail_list: true,
            },
            &FakeLinks {
                sources: Vec::new(),
                fail_block: false,
            },
            &FakeSpans {
                spans: Mutex::new(HashMap::new()),
                fail_get: false,
            },
        ))
        .expect_err("citations fail");
        assert!(matches!(error, Error::Storage(message) if message == "citations failed"));
    }

    #[test]
    fn test_link_failure_aborts_the_assembly() {
        let root = block("55555555-5555-5555-5555-555555555555", KEY_A, None, "Root.");
        let w = work();
        let rev = revision(w.id);
        let error = block_on(assemble_revision(
            &w,
            &rev,
            &FakeBlocks {
                tree: vec![root],
                fail_tree: false,
            },
            &FakeCitations {
                attached: Vec::new(),
                fail_list: false,
            },
            &FakeLinks {
                sources: Vec::new(),
                fail_block: true,
            },
            &FakeSpans {
                spans: Mutex::new(HashMap::new()),
                fail_get: false,
            },
        ))
        .expect_err("links fail");
        assert!(matches!(error, Error::Storage(message) if message == "links failed"));
    }

    #[test]
    fn test_span_failure_aborts_the_assembly() {
        let now = Utc::now();
        let span_id = Uuid::new_v4();
        let occurrence = CitationOccurrence {
            id: Uuid::new_v4(),
            citation_key: Uuid::parse_str(CITE_A).expect("fixture"),
            block_id: Uuid::parse_str("55555555-5555-5555-5555-555555555555").expect("fixture"),
            placement: Placement::Inline,
            intent: Intent::Quotation,
            note: None,
            created_at: now,
        };
        let item = CitationItem {
            occurrence_id: occurrence.id,
            position: 0,
            edition_id: None,
            edition_key: Some("DABAR_2026".to_owned()),
            source_span_id: Some(span_id),
            quoted_text: Some("Root.".to_owned()),
            verify_status: Some("exact".to_owned()),
            verified_at: None,
            locator: Map::new(),
            prefix: None,
            suffix: None,
            suppress_author: false,
        };
        let w = work();
        let rev = revision(w.id);
        let error = block_on(assemble_revision(
            &w,
            &rev,
            &FakeBlocks {
                tree: vec![block(
                    "55555555-5555-5555-5555-555555555555",
                    KEY_A,
                    None,
                    "Root.",
                )],
                fail_tree: false,
            },
            &FakeCitations {
                attached: vec![BlockCitations {
                    occurrence,
                    items: vec![item],
                }],
                fail_list: false,
            },
            &FakeLinks {
                sources: Vec::new(),
                fail_block: false,
            },
            &FakeSpans {
                spans: Mutex::new(HashMap::new()),
                fail_get: true,
            },
        ))
        .expect_err("spans fail");
        assert!(matches!(error, Error::Storage(message) if message == "span failed"));
    }
}
