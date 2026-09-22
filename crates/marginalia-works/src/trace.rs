//! Trace a work's grounding — blocks to spans to documents, and back.
//!
//! Python source: `services/works/trace.py`. Down from a work, block, or
//! citation: which corpus addresses it rests on. Up from a span or a
//! document: every block and anchor resting on it. Output is a tree of
//! labelled nodes, not prose.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use marginalia_types::citations::BlockCitations;
use marginalia_types::ports::{
    CitationRepo, DocumentRepo, SourceSpanRepo, WorkBlockRepo, WorkLinkRepo, WorkRepo,
    WorkRevisionRepo,
};
use marginalia_types::works::RevisionState;
use marginalia_types::works_files::Intent;
use marginalia_types::{Error, Result};

use crate::assembly::{assemble_revision, AssembledBlock, AssembledRevision};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TraceNode {
    pub kind: String,
    #[serde(default)]
    pub id: Option<String>,
    pub label: String,
    #[serde(default)]
    pub children: Vec<TraceNode>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct TraceSelector {
    pub slug: Option<String>,
    pub block_key: Option<String>,
    pub citation_key: Option<String>,
    pub source_span_id: Option<String>,
    pub document_id: Option<String>,
}

/// Parse a selector UUID or refuse like Python (`ValueError`, here
/// `Error::Validation`): `{name} is not a UUID: {raw}`.
pub fn parse_uuid_field(raw: &str, name: &str) -> Result<Uuid, Error> {
    // `Uuid::parse_str` accepts the same spellings `UUID(raw)` does
    // (hyphenated, braced, urn, simple); anything else is a bad selector.
    Uuid::parse_str(raw).map_err(|_| Error::Validation(format!("{name} is not a UUID: {raw}")))
}

fn revision_state_value(state: RevisionState) -> &'static str {
    match state {
        RevisionState::Draft => "draft",
        RevisionState::Frozen => "frozen",
        RevisionState::Published => "published",
        RevisionState::Superseded => "superseded",
    }
}

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

fn placement_value(placement: marginalia_types::works::Placement) -> &'static str {
    match placement {
        marginalia_types::works::Placement::Inline => "inline",
        marginalia_types::works::Placement::BlockEnd => "block_end",
    }
}

/// One block down: its occurrences, source links, entity links, then child
/// blocks in tree order. Pure over the assembled view.
pub fn block_down(item: &AssembledBlock, view: &AssembledRevision) -> TraceNode {
    let block = &item.block;
    let mut children: Vec<TraceNode> = item
        .citations
        .iter()
        .map(|entry| occurrence_down(view, entry))
        .collect();
    let sources = item
        .links
        .as_ref()
        .map_or(&[][..], |links| &links.sources[..]);
    for link in sources {
        let span = view.spans.get(&link.source_span_id);
        children.push(TraceNode {
            kind: "source_link".to_owned(),
            id: Some(link.source_span_id.to_string()),
            label: format!(
                "{}{}",
                link.relation,
                span.map_or(String::new(), |span| format!(
                    " [{}, {})",
                    span.char_start, span.char_end
                ))
            ),
            children: Vec::new(),
        });
    }
    let entities = item
        .links
        .as_ref()
        .map_or(&[][..], |links| &links.entities[..]);
    for link in entities {
        children.push(TraceNode {
            kind: "entity_link".to_owned(),
            id: Some(link.entity_id.to_string()),
            label: format!(
                "{}{}",
                link.relation,
                match &link.surface_form {
                    Some(surface) if !surface.is_empty() => format!(" «{surface}»"),
                    _ => String::new(),
                }
            ),
            children: Vec::new(),
        });
    }
    for child in &view.blocks {
        if child.block.parent_id == Some(block.id) {
            children.push(block_down(child, view));
        }
    }
    let title = match &block.title {
        Some(title) if !title.is_empty() => format!(" «{title}»"),
        _ => String::new(),
    };
    TraceNode {
        kind: "block".to_owned(),
        id: Some(block.block_key.to_string()),
        label: format!("{}{}", block.block_type, title),
        children,
    }
}

/// One occurrence down: spans with verify status, or bibliography stubs.
/// Pure over the assembled view.
pub fn occurrence_down(view: &AssembledRevision, entry: &BlockCitations) -> TraceNode {
    let occurrence = &entry.occurrence;
    let mut children = Vec::new();
    for row in &entry.items {
        if let Some(span_id) = row.source_span_id {
            if let Some(span) = view.spans.get(&span_id) {
                children.push(TraceNode {
                    kind: "source_span".to_owned(),
                    id: Some(span.id.to_string()),
                    label: format!(
                        "[{}, {}) {}",
                        span.char_start,
                        span.char_end,
                        row.verify_status.as_deref().unwrap_or("unverified")
                    ),
                    children: Vec::new(),
                });
            }
        } else if let Some(edition_key) = &row.edition_key {
            children.push(TraceNode {
                kind: "bibliography".to_owned(),
                id: None,
                label: format!("{edition_key} (no span)"),
                children: Vec::new(),
            });
        }
        // Other spanless items name neither a span nor an edition: they hang
        // nothing under the occurrence, exactly as Python's loop skips them.
    }
    TraceNode {
        kind: "citation".to_owned(),
        id: Some(occurrence.citation_key.to_string()),
        label: format!(
            "{} {}",
            intent_value(occurrence.intent),
            placement_value(occurrence.placement)
        ),
        children,
    }
}

/// One selector in, a grounding tree out.
pub struct WorkTraceService<W, R, B, C, L, S, D> {
    works: W,
    revisions: R,
    blocks: B,
    citations: C,
    links: L,
    spans: S,
    documents: D,
}

impl<W, R, B, C, L, S, D> WorkTraceService<W, R, B, C, L, S, D>
where
    W: WorkRepo,
    R: WorkRevisionRepo,
    B: WorkBlockRepo,
    C: CitationRepo,
    L: WorkLinkRepo,
    S: SourceSpanRepo,
    D: DocumentRepo,
{
    pub fn new(
        works: W,
        revisions: R,
        blocks: B,
        citations: C,
        links: L,
        spans: S,
        documents: D,
    ) -> Self {
        Self {
            works,
            revisions,
            blocks,
            citations,
            links,
            spans,
            documents,
        }
    }

    /// Walk down from authored rows or up from corpus addresses. Takes
    /// exactly one selector; anything else is a `Validation` error with the
    /// Python `ValueError` text.
    pub async fn trace(&self, selector: TraceSelector) -> Result<TraceNode, Error> {
        let given = [
            selector.slug.is_some(),
            selector.block_key.is_some(),
            selector.citation_key.is_some(),
            selector.source_span_id.is_some(),
            selector.document_id.is_some(),
        ]
        .into_iter()
        .filter(|present| *present)
        .count();
        if given != 1 {
            return Err(Error::Validation(
                "trace takes exactly one selector: slug, block_key, citation_key, source_span_id, or document_id"
                    .to_owned(),
            ));
        }
        if let Some(slug) = selector.slug {
            return self.trace_work(&slug).await;
        }
        if let Some(raw_key) = selector.block_key {
            return self.trace_block(&raw_key).await;
        }
        if let Some(raw_key) = selector.citation_key {
            return self.trace_citation(&raw_key).await;
        }
        if let Some(raw_id) = selector.source_span_id {
            return self.trace_span(&raw_id).await;
        }
        // proof: exactly one selector is set, and the four above are absent.
        let raw_id = selector
            .document_id
            .expect("trace selector holds a document_id");
        self.trace_document(&raw_id).await
    }

    async fn trace_work(&self, slug: &str) -> Result<TraceNode, Error> {
        let work = self
            .works
            .get_by_slug(slug)
            .await?
            .ok_or_else(|| Error::NotFound {
                kind: "work",
                id: slug.to_owned(),
            })?;
        let current_id = work.current_revision_id.ok_or_else(|| Error::NotFound {
            kind: "work_revision",
            id: format!("current of {slug}"),
        })?;
        let revision = self
            .revisions
            .get(current_id)
            .await?
            .ok_or_else(|| Error::NotFound {
                kind: "work_revision",
                id: current_id.to_string(),
            })?;
        let view = assemble_revision(
            &work,
            &revision,
            &self.blocks,
            &self.citations,
            &self.links,
            &self.spans,
        )
        .await?;
        let roots = view
            .blocks
            .iter()
            .filter(|item| item.block.parent_id.is_none());
        Ok(TraceNode {
            kind: "work".to_owned(),
            id: Some(work.id.to_string()),
            label: format!(
                "{} rev {} ({})",
                work.slug,
                revision.revision_number,
                revision_state_value(revision.state)
            ),
            children: roots.map(|item| block_down(item, &view)).collect(),
        })
    }

    async fn trace_block(&self, raw_key: &str) -> Result<TraceNode, Error> {
        let key = parse_uuid_field(raw_key, "block_key")?;
        let found = self.find_block(key).await?;
        let (_, _, view, item) = found.ok_or_else(|| Error::NotFound {
            kind: "work_block",
            id: raw_key.to_owned(),
        })?;
        Ok(block_down(&item, &view))
    }

    async fn trace_citation(&self, raw_key: &str) -> Result<TraceNode, Error> {
        let key = parse_uuid_field(raw_key, "citation_key")?;
        // Occurrences are addressed per revision: search the works' current
        // revisions for the key. Works are few by nature, so this fan-out
        // stays small.
        for work in self.works.list().await? {
            let Some(current_id) = work.current_revision_id else {
                continue;
            };
            let Some(revision) = self.revisions.get(current_id).await? else {
                continue;
            };
            if let Some(entry) = self.citations.by_key(revision.id, key).await? {
                let view = assemble_revision(
                    &work,
                    &revision,
                    &self.blocks,
                    &self.citations,
                    &self.links,
                    &self.spans,
                )
                .await?;
                return Ok(occurrence_down(&view, &entry));
            }
        }
        Err(Error::NotFound {
            kind: "citation_occurrence",
            id: raw_key.to_owned(),
        })
    }

    async fn trace_span(&self, raw_id: &str) -> Result<TraceNode, Error> {
        let span_id = parse_uuid_field(raw_id, "source_span_id")?;
        let span = self
            .spans
            .get(span_id)
            .await?
            .ok_or_else(|| Error::NotFound {
                kind: "source_span",
                id: raw_id.to_owned(),
            })?;
        let mut node = self.span_down(&span).await;
        node.children.extend(self.span_up(span_id).await?);
        Ok(node)
    }

    async fn trace_document(&self, raw_id: &str) -> Result<TraceNode, Error> {
        let document_id = parse_uuid_field(raw_id, "document_id")?;
        let document = self
            .documents
            .get(document_id)
            .await?
            .ok_or_else(|| Error::NotFound {
                kind: "document",
                id: raw_id.to_owned(),
            })?;
        let mut children = Vec::new();
        for span in self.spans.for_document(document_id).await? {
            let up = self.span_up(span.id).await?;
            children.push(TraceNode {
                kind: "source_span".to_owned(),
                id: Some(span.id.to_string()),
                label: format!("[{}, {})", span.char_start, span.char_end),
                children: up,
            });
        }
        Ok(TraceNode {
            kind: "document".to_owned(),
            id: Some(document_id.to_string()),
            label: document
                .title
                .clone()
                .unwrap_or_else(|| document_id.to_string()),
            children,
        })
    }

    async fn span_down(&self, span: &marginalia_types::spans::SourceSpan) -> TraceNode {
        let document = self.documents.get(span.document_id).await.ok().flatten();
        let doc_label = match document {
            Some(document) => document
                .title
                .unwrap_or_else(|| span.document_id.to_string()),
            None => span.document_id.to_string(),
        };
        TraceNode {
            kind: "source_span".to_owned(),
            id: Some(span.id.to_string()),
            label: format!("[{}, {})", span.char_start, span.char_end),
            children: vec![TraceNode {
                kind: "document".to_owned(),
                id: Some(span.document_id.to_string()),
                label: format!(
                    "{doc_label} offsets {}\u{2013}{}",
                    span.char_start, span.char_end
                ),
                children: Vec::new(),
            }],
        }
    }

    /// Every block and anchor resting on this span.
    async fn span_up(&self, span_id: Uuid) -> Result<Vec<TraceNode>, Error> {
        let mut nodes = Vec::new();
        for entry in self.citations.citing_span(span_id).await? {
            let block = self.blocks.get(entry.occurrence.block_id).await?;
            let mut label = entry.occurrence.citation_key.to_string();
            if let Some(block) = block {
                label.push_str(&format!(
                    " in block {} ({})",
                    block.block_key, block.block_type
                ));
                if let Some(work_slug) = self.slug_of_revision(block.revision_id).await? {
                    label.push_str(&format!(" of {work_slug}"));
                }
            }
            nodes.push(TraceNode {
                kind: "citation".to_owned(),
                id: Some(entry.occurrence.citation_key.to_string()),
                label,
                children: Vec::new(),
            });
        }
        for link in self.links.for_span(span_id).await? {
            let block = self.blocks.get(link.block_id).await?;
            let mut label = link.relation.clone();
            if let Some(block) = block {
                label.push_str(&format!(
                    " from block {} ({})",
                    block.block_key, block.block_type
                ));
            }
            nodes.push(TraceNode {
                kind: "source_link".to_owned(),
                id: Some(span_id.to_string()),
                label,
                children: Vec::new(),
            });
        }
        Ok(nodes)
    }

    async fn find_block(
        &self,
        key: Uuid,
    ) -> Result<
        Option<(
            marginalia_types::works::Work,
            marginalia_types::works::WorkRevision,
            AssembledRevision,
            AssembledBlock,
        )>,
        Error,
    > {
        for work in self.works.list().await? {
            let Some(current_id) = work.current_revision_id else {
                continue;
            };
            let Some(revision) = self.revisions.get(current_id).await? else {
                continue;
            };
            if let Some(block) = self.blocks.by_key(revision.id, key).await? {
                let view = assemble_revision(
                    &work,
                    &revision,
                    &self.blocks,
                    &self.citations,
                    &self.links,
                    &self.spans,
                )
                .await?;
                for item in &view.blocks {
                    if item.block.id == block.id {
                        return Ok(Some((work, revision, view.clone(), item.clone())));
                    }
                }
            }
        }
        Ok(None)
    }

    async fn slug_of_revision(&self, revision_id: Uuid) -> Result<Option<String>, Error> {
        let Some(revision) = self.revisions.get(revision_id).await? else {
            return Ok(None);
        };
        let work = self.works.get(revision.work_id).await?;
        Ok(work.map(|work| work.slug))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::future::Future;
    use std::pin::Pin;
    use std::sync::Mutex;
    use std::task::{Context, Poll, RawWaker, RawWakerVTable, Waker};

    use chrono::Utc;
    use marginalia_types::citations::{
        BlockCitations, CitationItem, CitationItemDraft, CitationOccurrence, OccurrenceDraft,
    };
    use marginalia_types::documents::Document;
    use marginalia_types::spans::SourceSpan;
    use marginalia_types::works::{
        BlockEntityLink, BlockLinks, BlockSourceLink, Placement, RevisionState, Work, WorkBlock,
        WorkRevision, WorkStatus,
    };
    use marginalia_types::{Error, Result};

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

    const WORK_ID: &str = "33333333-3333-3333-3333-333333333333";
    const REV_ID: &str = "44444444-4444-4444-4444-444444444444";
    const BLOCK_ID: &str = "55555555-5555-5555-5555-555555555555";
    const BLOCK_KEY: &str = "11111111-1111-1111-1111-111111111111";
    const CITE_KEY: &str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    const SPAN_ID: &str = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
    const DOC_ID: &str = "cccccccc-cccc-cccc-cccc-cccccccccccc";

    fn uuid(raw: &str) -> Uuid {
        Uuid::parse_str(raw).expect("fixture uuid")
    }

    fn work() -> Work {
        let now = Utc::now();
        Work {
            id: uuid(WORK_ID),
            slug: "deror".to_owned(),
            title: "Deror".to_owned(),
            work_type: "essay".to_owned(),
            status: WorkStatus::Draft,
            language: None,
            abstract_text: None,
            current_revision_id: Some(uuid(REV_ID)),
            metadata: serde_json::Map::new(),
            created_at: now,
            updated_at: now,
            archived_at: None,
        }
    }

    fn revision() -> WorkRevision {
        let now = Utc::now();
        WorkRevision {
            id: uuid(REV_ID),
            work_id: uuid(WORK_ID),
            revision_number: 1,
            parent_revision_id: None,
            state: RevisionState::Draft,
            message: None,
            content_hash: None,
            created_by: "user".to_owned(),
            created_at: now,
            frozen_at: None,
            published_at: None,
            metadata: serde_json::Map::new(),
        }
    }

    fn work_block() -> WorkBlock {
        let now = Utc::now();
        WorkBlock {
            id: uuid(BLOCK_ID),
            revision_id: uuid(REV_ID),
            block_key: uuid(BLOCK_KEY),
            parent_id: None,
            position: 0,
            block_type: "paragraph".to_owned(),
            title: None,
            body_markdown: "The prophets speak.".to_owned(),
            attributes: serde_json::Map::new(),
            created_at: now,
            updated_at: now,
        }
    }

    fn span() -> SourceSpan {
        SourceSpan {
            id: uuid(SPAN_ID),
            document_id: uuid(DOC_ID),
            char_start: 34,
            char_end: 62,
            quoted_text: "The prophets pair two words.".to_owned(),
            parser: None,
            parser_version: None,
            passage_id: None,
            created_at: Utc::now(),
        }
    }

    fn document() -> Document {
        Document {
            id: uuid(DOC_ID),
            title: Some("Amos".to_owned()),
            document_type: "scripture".to_owned(),
            language: None,
            source: "test://amos".to_owned(),
            content_hash: vec![0],
            parser: "test".to_owned(),
            parser_version: "1".to_owned(),
            ingested_at: Utc::now(),
            created_date_start: None,
            created_date_end: None,
            created_precision: None,
            edition_id: None,
            metadata: serde_json::Map::new(),
        }
    }

    fn occurrence() -> CitationOccurrence {
        CitationOccurrence {
            id: Uuid::new_v4(),
            citation_key: uuid(CITE_KEY),
            block_id: uuid(BLOCK_ID),
            placement: Placement::Inline,
            intent: Intent::Quotation,
            note: None,
            created_at: Utc::now(),
        }
    }

    fn item_with_span() -> CitationItem {
        CitationItem {
            occurrence_id: uuid(CITE_KEY),
            position: 0,
            edition_id: None,
            edition_key: Some("DABAR_2026".to_owned()),
            source_span_id: Some(uuid(SPAN_ID)),
            quoted_text: Some("The prophets pair two words.".to_owned()),
            verify_status: Some("exact".to_owned()),
            verified_at: None,
            locator: serde_json::Map::new(),
            prefix: None,
            suffix: None,
            suppress_author: false,
        }
    }

    fn entry() -> BlockCitations {
        BlockCitations {
            occurrence: occurrence(),
            items: vec![item_with_span()],
        }
    }

    fn assembled() -> AssembledRevision {
        let work = work();
        AssembledRevision {
            revision: revision(),
            work,
            blocks: vec![AssembledBlock {
                block: work_block(),
                parent_key: None,
                citations: vec![entry()],
                links: Some(BlockLinks::default()),
            }],
            spans: HashMap::from([(uuid(SPAN_ID), span())]),
        }
    }

    struct Fakes {
        works: Vec<Work>,
        revisions: HashMap<Uuid, WorkRevision>,
        blocks: Vec<WorkBlock>,
        entries: Vec<BlockCitations>,
        spans: HashMap<Uuid, SourceSpan>,
        documents: HashMap<Uuid, Document>,
        links_for_span: Vec<BlockSourceLink>,
        fail_list: bool,
        fail_slug: bool,
        fail_revision_get: bool,
        fail_work_get: bool,
        fail_block_by_key: bool,
        fail_blocks_get: bool,
        fail_citation_by_key: bool,
        fail_citing_span: bool,
        fail_links_for_span: bool,
        fail_spans_get: bool,
        fail_spans_for_document: bool,
        fail_documents_get: bool,
        fail_tree: bool,
    }

    impl Fakes {
        fn with_work() -> Self {
            Self {
                works: vec![work()],
                revisions: HashMap::from([(uuid(REV_ID), revision())]),
                blocks: vec![work_block()],
                entries: vec![entry()],
                spans: HashMap::from([(uuid(SPAN_ID), span())]),
                documents: HashMap::from([(uuid(DOC_ID), document())]),
                links_for_span: Vec::new(),
                fail_list: false,
                fail_slug: false,
                fail_revision_get: false,
                fail_work_get: false,
                fail_block_by_key: false,
                fail_blocks_get: false,
                fail_citation_by_key: false,
                fail_citing_span: false,
                fail_links_for_span: false,
                fail_spans_get: false,
                fail_spans_for_document: false,
                fail_documents_get: false,
                fail_tree: false,
            }
        }
    }

    struct Shared<'a>(&'a Mutex<Fakes>);

    impl<'a> WorkRepo for Shared<'a> {
        type Tx = ();
        async fn insert(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::works::WorkDraft,
        ) -> Result<Work> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn get(&self, work_id: Uuid) -> Result<Option<Work>> {
            if self.0.lock().expect("lock").fail_work_get {
                return Err(Error::Storage("work failed".to_owned()));
            }
            Ok(self
                .0
                .lock()
                .expect("lock")
                .works
                .iter()
                .find(|w| w.id == work_id)
                .cloned())
        }
        async fn get_by_slug(&self, slug: &str) -> Result<Option<Work>> {
            if self.0.lock().expect("lock").fail_slug {
                return Err(Error::Storage("works failed".to_owned()));
            }
            Ok(self
                .0
                .lock()
                .expect("lock")
                .works
                .iter()
                .find(|w| w.slug == slug)
                .cloned())
        }
        async fn list(&self) -> Result<Vec<Work>> {
            if self.0.lock().expect("lock").fail_list {
                return Err(Error::Storage("list failed".to_owned()));
            }
            Ok(self.0.lock().expect("lock").works.clone())
        }
        async fn set_current_revision(
            &self,
            _tx: &mut Self::Tx,
            _work_id: Uuid,
            _revision_id: Uuid,
        ) -> Result<()> {
            Ok(())
        }
        async fn update(
            &self,
            _tx: &mut Self::Tx,
            _work_id: Uuid,
            _expected_updated_at: chrono::DateTime<Utc>,
            _fields: serde_json::Map<String, serde_json::Value>,
        ) -> Result<Work> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn archive(&self, _tx: &mut Self::Tx, _work_id: Uuid) -> Result<Work> {
            Err(Error::Storage("unused".to_owned()))
        }
    }

    impl<'a> WorkRevisionRepo for Shared<'a> {
        type Tx = ();
        async fn insert(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::works::WorkRevisionDraft,
        ) -> Result<WorkRevision> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn get(&self, revision_id: Uuid) -> Result<Option<WorkRevision>> {
            if self.0.lock().expect("lock").fail_revision_get {
                return Err(Error::Storage("revisions failed".to_owned()));
            }
            Ok(self
                .0
                .lock()
                .expect("lock")
                .revisions
                .get(&revision_id)
                .cloned())
        }
        async fn latest(&self, _work_id: Uuid) -> Result<Option<WorkRevision>> {
            Ok(None)
        }
        async fn copy_forward(
            &self,
            _tx: &mut Self::Tx,
            _revision_id: Uuid,
        ) -> Result<WorkRevision> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn set_message(
            &self,
            _tx: &mut Self::Tx,
            _revision_id: Uuid,
            _message: &str,
        ) -> Result<WorkRevision> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn freeze(
            &self,
            _tx: &mut Self::Tx,
            _revision_id: Uuid,
            _content_hash: &[u8],
        ) -> Result<WorkRevision> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn publish(&self, _tx: &mut Self::Tx, _revision_id: Uuid) -> Result<WorkRevision> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn supersede(&self, _tx: &mut Self::Tx, _revision_id: Uuid) -> Result<WorkRevision> {
            Err(Error::Storage("unused".to_owned()))
        }
    }

    impl<'a> WorkBlockRepo for Shared<'a> {
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
            if self.0.lock().expect("lock").fail_tree {
                return Err(Error::Storage("tree failed".to_owned()));
            }
            Ok(self.0.lock().expect("lock").blocks.clone())
        }
        async fn get(&self, block_id: Uuid) -> Result<Option<WorkBlock>> {
            if self.0.lock().expect("lock").fail_blocks_get {
                return Err(Error::Storage("blocks failed".to_owned()));
            }
            Ok(self
                .0
                .lock()
                .expect("lock")
                .blocks
                .iter()
                .find(|b| b.id == block_id)
                .cloned())
        }
        async fn by_key(&self, _revision_id: Uuid, block_key: Uuid) -> Result<Option<WorkBlock>> {
            if self.0.lock().expect("lock").fail_block_by_key {
                return Err(Error::Storage("blocks failed".to_owned()));
            }
            Ok(self
                .0
                .lock()
                .expect("lock")
                .blocks
                .iter()
                .find(|b| b.block_key == block_key)
                .cloned())
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

    impl<'a> CitationRepo for Shared<'a> {
        type Tx = ();
        async fn insert_occurrence(
            &self,
            _tx: &mut Self::Tx,
            _draft: OccurrenceDraft,
        ) -> Result<CitationOccurrence> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn insert_item(
            &self,
            _tx: &mut Self::Tx,
            _draft: CitationItemDraft,
        ) -> Result<CitationItem> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn for_block(&self, _block_id: Uuid) -> Result<Vec<BlockCitations>> {
            Ok(Vec::new())
        }
        async fn for_revision(&self, _revision_id: Uuid) -> Result<Vec<BlockCitations>> {
            Ok(self.0.lock().expect("lock").entries.clone())
        }
        async fn by_key(
            &self,
            _revision_id: Uuid,
            citation_key: Uuid,
        ) -> Result<Option<BlockCitations>> {
            if self.0.lock().expect("lock").fail_citation_by_key {
                return Err(Error::Storage("citations failed".to_owned()));
            }
            Ok(self
                .0
                .lock()
                .expect("lock")
                .entries
                .iter()
                .find(|e| e.occurrence.citation_key == citation_key)
                .cloned())
        }
        async fn citing_span(&self, span_id: Uuid) -> Result<Vec<BlockCitations>> {
            if self.0.lock().expect("lock").fail_citing_span {
                return Err(Error::Storage("citations failed".to_owned()));
            }
            Ok(self
                .0
                .lock()
                .expect("lock")
                .entries
                .iter()
                .filter(|e| {
                    e.items
                        .iter()
                        .any(|row| row.source_span_id == Some(span_id))
                })
                .cloned()
                .collect())
        }
        async fn citing_key(&self, _edition_key: &str) -> Result<Vec<BlockCitations>> {
            Ok(Vec::new())
        }
    }

    impl<'a> WorkLinkRepo for Shared<'a> {
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
            Ok(BlockLinks::default())
        }
        async fn for_span(&self, _span_id: Uuid) -> Result<Vec<BlockSourceLink>> {
            if self.0.lock().expect("lock").fail_links_for_span {
                return Err(Error::Storage("links failed".to_owned()));
            }
            Ok(self.0.lock().expect("lock").links_for_span.clone())
        }
        async fn for_entity(
            &self,
            _entity_id: Uuid,
            _relation: &str,
        ) -> Result<Vec<BlockEntityLink>> {
            Ok(Vec::new())
        }
    }

    impl<'a> SourceSpanRepo for Shared<'a> {
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
            if self.0.lock().expect("lock").fail_spans_get {
                return Err(Error::Storage("spans failed".to_owned()));
            }
            Ok(self.0.lock().expect("lock").spans.get(&span_id).cloned())
        }
        async fn for_document(&self, document_id: Uuid) -> Result<Vec<SourceSpan>> {
            if self.0.lock().expect("lock").fail_spans_for_document {
                return Err(Error::Storage("spans failed".to_owned()));
            }
            Ok(self
                .0
                .lock()
                .expect("lock")
                .spans
                .values()
                .filter(|s| s.document_id == document_id)
                .cloned()
                .collect())
        }
        async fn stale(&self, _limit: i64) -> Result<Vec<SourceSpan>> {
            Ok(Vec::new())
        }
    }

    impl<'a> DocumentRepo for Shared<'a> {
        type Tx = ();
        async fn insert(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::documents::DocumentDraft,
        ) -> Result<Document> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn get(&self, doc_id: Uuid) -> Result<Option<Document>> {
            if self.0.lock().expect("lock").fail_documents_get {
                return Err(Error::Storage("documents failed".to_owned()));
            }
            Ok(self.0.lock().expect("lock").documents.get(&doc_id).cloned())
        }
        async fn get_many(&self, _doc_ids: &[Uuid]) -> Result<Vec<Document>> {
            Ok(Vec::new())
        }
        async fn find_by_hash(
            &self,
            _content_hash: &[u8],
            _source: &str,
        ) -> Result<Option<Document>> {
            Ok(None)
        }
        async fn find_by_edition_id(
            &self,
            _tx: &mut Self::Tx,
            _edition_id: Uuid,
        ) -> Result<Option<Document>> {
            Ok(None)
        }
        async fn update_metadata(
            &self,
            _doc_id: Uuid,
            _patch: serde_json::Map<String, serde_json::Value>,
        ) -> Result<Document> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn iter_by_filter(
            &self,
            _filter: &marginalia_types::documents::DocumentFilter,
        ) -> Result<Vec<Document>> {
            Ok(Vec::new())
        }
        async fn count(
            &self,
            _filter: Option<&marginalia_types::documents::DocumentFilter>,
        ) -> Result<i64> {
            Ok(0)
        }
        async fn delete(&self, _doc_id: Uuid) -> Result<()> {
            Ok(())
        }
    }

    fn service<'a>(
        fakes: &'a Mutex<Fakes>,
    ) -> WorkTraceService<
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
    > {
        WorkTraceService::new(
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
        )
    }

    fn selector() -> TraceSelector {
        TraceSelector::default()
    }

    #[test]
    fn test_parse_uuid_field_refuses_with_name_and_raw() {
        let error = parse_uuid_field("nope", "block_key").expect_err("bad uuid");
        assert!(
            matches!(error, Error::Validation(message) if message == "block_key is not a UUID: nope")
        );
    }

    #[test]
    fn test_trace_needs_exactly_one_selector() {
        let fakes = Mutex::new(Fakes::with_work());
        let service = service(&fakes);
        let error = block_on(service.trace(selector())).expect_err("no selector");
        assert!(matches!(error, Error::Validation(message) if message
                == "trace takes exactly one selector: slug, block_key, citation_key, source_span_id, or document_id"));
        let mut two = selector();
        two.slug = Some("deror".to_owned());
        two.block_key = Some(BLOCK_KEY.to_owned());
        let error = block_on(service.trace(two)).expect_err("two selectors");
        assert_eq!(
            error.to_string(),
            "data validation failed: trace takes exactly one selector: slug, block_key, citation_key, source_span_id, or document_id"
        );
    }

    #[test]
    fn test_trace_work_labels_revision_and_reaches_blocks() {
        // Mirrors `test_trace`'s down leg: kind work, children reach blocks.
        let fakes = Mutex::new(Fakes::with_work());
        let service = service(&fakes);
        let mut sel = selector();
        sel.slug = Some("deror".to_owned());
        let node = block_on(service.trace(sel)).expect("trace work");
        assert_eq!(node.kind, "work");
        assert_eq!(node.id.as_deref(), Some(WORK_ID));
        assert_eq!(node.label, "deror rev 1 (draft)");
        assert_eq!(node.children.len(), 1);
        let block = &node.children[0];
        assert_eq!(block.kind, "block");
        assert_eq!(block.id.as_deref(), Some(BLOCK_KEY));
        assert_eq!(block.label, "paragraph");
        assert_eq!(block.children.len(), 1);
        let citation = &block.children[0];
        assert_eq!(citation.kind, "citation");
        assert_eq!(citation.id.as_deref(), Some(CITE_KEY));
        assert_eq!(citation.label, "quotation inline");
        assert_eq!(citation.children.len(), 1);
        assert_eq!(citation.children[0].label, "[34, 62) exact");
    }

    #[test]
    fn test_trace_block_titles_and_missing_block() {
        let fakes = Mutex::new(Fakes::with_work());
        {
            let mut guard = fakes.lock().expect("lock");
            guard.blocks[0].title = Some("Release".to_owned());
        }
        let service = service(&fakes);
        let mut sel = selector();
        sel.block_key = Some(BLOCK_KEY.to_owned());
        let node = block_on(service.trace(sel)).expect("trace block");
        assert_eq!(node.kind, "block");
        assert_eq!(node.label, "paragraph «Release»");

        let mut missing = selector();
        missing.block_key = Some("99999999-9999-9999-9999-999999999999".to_owned());
        let error = block_on(service.trace(missing)).expect_err("missing block");
        assert!(matches!(
            error,
            Error::NotFound {
                kind: "work_block",
                ..
            }
        ));
    }

    #[test]
    fn test_trace_citation_fans_out_over_current_revisions() {
        let fakes = Mutex::new(Fakes::with_work());
        let service = service(&fakes);
        let mut sel = selector();
        sel.citation_key = Some(CITE_KEY.to_owned());
        let node = block_on(service.trace(sel)).expect("trace citation");
        assert_eq!(node.kind, "citation");
        assert_eq!(node.id.as_deref(), Some(CITE_KEY));
        assert_eq!(node.label, "quotation inline");
        assert_eq!(node.children.len(), 1);
        assert_eq!(node.children[0].kind, "source_span");
    }

    #[test]
    fn test_trace_span_names_document_with_en_dash_and_citers() {
        // Mirrors `test_trace`'s up leg: the span names its citing occurrence,
        // and the document child joins offsets with U+2013.
        let fakes = Mutex::new(Fakes::with_work());
        let service = service(&fakes);
        let mut sel = selector();
        sel.source_span_id = Some(SPAN_ID.to_owned());
        let node = block_on(service.trace(sel)).expect("trace span");
        assert_eq!(node.kind, "source_span");
        assert_eq!(node.id.as_deref(), Some(SPAN_ID));
        assert_eq!(node.label, "[34, 62)");
        assert_eq!(node.children.len(), 2);
        assert_eq!(node.children[0].kind, "document");
        assert_eq!(node.children[0].label, "Amos offsets 34\u{2013}62");
        let citing = &node.children[1];
        assert_eq!(citing.kind, "citation");
        assert_eq!(citing.id.as_deref(), Some(CITE_KEY));
        assert_eq!(
            citing.label,
            format!("{CITE_KEY} in block {BLOCK_KEY} (paragraph) of deror")
        );
    }

    #[test]
    fn test_trace_document_wraps_spans() {
        let fakes = Mutex::new(Fakes::with_work());
        let service = service(&fakes);
        let mut sel = selector();
        sel.document_id = Some(DOC_ID.to_owned());
        let node = block_on(service.trace(sel)).expect("trace document");
        assert_eq!(node.kind, "document");
        assert_eq!(node.label, "Amos");
        assert_eq!(node.children.len(), 1);
        assert_eq!(node.children[0].kind, "source_span");
        assert_eq!(node.children[0].label, "[34, 62)");
        assert!(node.children[0]
            .children
            .iter()
            .any(|child| child.kind == "citation" && child.id.as_deref() == Some(CITE_KEY)));
    }

    #[test]
    fn test_occurrence_down_bibliography_and_spanless_silence() {
        // A spanless item with an edition key hangs a bibliography stub; a
        // spanless item with neither names nothing.
        let mut edition_only = item_with_span();
        edition_only.source_span_id = None;
        edition_only.verify_status = None;
        let mut bare = item_with_span();
        bare.source_span_id = None;
        bare.edition_key = None;
        bare.verify_status = None;
        let view = assembled();
        let node = occurrence_down(
            &view,
            &BlockCitations {
                occurrence: occurrence(),
                items: vec![edition_only, bare],
            },
        );
        assert_eq!(node.kind, "citation");
        assert_eq!(node.children.len(), 1);
        assert_eq!(node.children[0].kind, "bibliography");
        assert_eq!(node.children[0].label, "DABAR_2026 (no span)");
        assert_eq!(node.children[0].id, None);
    }

    #[test]
    fn test_source_link_labels_name_cached_span_bounds() {
        // A source link whose span missed the cache labels the bare
        // relation; a cached span appends its bounds, mirroring the
        // occurrence span labels.
        let mut view = assembled();
        view.blocks[0].links = Some(BlockLinks {
            sources: vec![
                BlockSourceLink {
                    block_id: uuid(BLOCK_ID),
                    source_span_id: Uuid::new_v4(),
                    relation: "discusses".to_owned(),
                    confidence: None,
                    note: None,
                    created_at: Utc::now(),
                },
                BlockSourceLink {
                    block_id: uuid(BLOCK_ID),
                    source_span_id: uuid(SPAN_ID),
                    relation: "cites".to_owned(),
                    confidence: None,
                    note: None,
                    created_at: Utc::now(),
                },
            ],
            entities: vec![BlockEntityLink {
                block_id: uuid(BLOCK_ID),
                entity_id: Uuid::new_v4(),
                relation: "renders".to_owned(),
                surface_form: Some("logos".to_owned()),
                created_at: Utc::now(),
            }],
        });
        let node = block_down(&view.blocks[0].clone(), &view);
        let kinds: Vec<&str> = node
            .children
            .iter()
            .map(|child| child.kind.as_str())
            .collect();
        assert!(kinds.contains(&"source_link"));
        assert!(kinds.contains(&"entity_link"));
        let link = node
            .children
            .iter()
            .find(|child| child.kind == "source_link")
            .expect("source link");
        assert_eq!(link.label, "discusses");
        let cached = node
            .children
            .iter()
            .find(|child| child.kind == "source_link" && child.label.starts_with("cites"))
            .expect("cached source link");
        assert_eq!(cached.label, "cites [34, 62)");
        let entity = node
            .children
            .iter()
            .find(|child| child.kind == "entity_link")
            .expect("entity link");
        assert_eq!(entity.label, "renders «logos»");
    }

    #[test]
    fn test_missing_work_and_bad_selector_uuid() {
        let fakes = Mutex::new(Fakes::with_work());
        let service = service(&fakes);
        let mut missing = selector();
        missing.slug = Some("unknown".to_owned());
        let error = block_on(service.trace(missing)).expect_err("missing work");
        assert!(matches!(error, Error::NotFound { kind: "work", .. }));

        let mut bad = selector();
        bad.source_span_id = Some("nope".to_owned());
        let error = block_on(service.trace(bad)).expect_err("bad uuid");
        assert!(
            matches!(error, Error::Validation(message) if message == "source_span_id is not a UUID: nope")
        );
    }
    #[test]
    fn test_revision_states_label_the_work() {
        // The work label interpolates `RevisionState.value`, mirroring the
        // Python trace root; draft is pinned by the main work test.
        for (state, name) in [
            (RevisionState::Frozen, "frozen"),
            (RevisionState::Published, "published"),
            (RevisionState::Superseded, "superseded"),
        ] {
            let mut rig = Fakes::with_work();
            rig.revisions
                .get_mut(&uuid(REV_ID))
                .expect("revision")
                .state = state;
            let fakes = Mutex::new(rig);
            let service = service(&fakes);
            let mut sel = selector();
            sel.slug = Some("deror".to_owned());
            let node = block_on(service.trace(sel)).expect("trace work");
            assert_eq!(node.label, format!("deror rev 1 ({name})"));
        }
    }

    #[test]
    fn test_occurrence_labels_name_every_intent_and_placement() {
        // The citation label is `{intent.value} {placement.value}`; the
        // quotation/inline pair is pinned by the main trace tests.
        let view = assembled();
        for (intent, name) in [
            (Intent::Translation, "translation"),
            (Intent::Support, "support"),
            (Intent::Contrast, "contrast"),
            (Intent::Background, "background"),
            (Intent::Definition, "definition"),
            (Intent::Source, "source"),
            (Intent::SeeAlso, "see_also"),
        ] {
            let mut resolved = occurrence();
            resolved.intent = intent;
            resolved.placement = Placement::BlockEnd;
            let node = occurrence_down(
                &view,
                &BlockCitations {
                    occurrence: resolved,
                    items: Vec::new(),
                },
            );
            assert_eq!(node.kind, "citation");
            assert_eq!(node.label, format!("{name} block_end"));
            assert!(node.children.is_empty());
        }
        // A located span without a verify status traces as unverified.
        let mut bare = item_with_span();
        bare.verify_status = None;
        let node = occurrence_down(
            &view,
            &BlockCitations {
                occurrence: occurrence(),
                items: vec![bare],
            },
        );
        assert_eq!(node.children.len(), 1);
        assert_eq!(node.children[0].label, "[34, 62) unverified");
    }

    #[test]
    fn test_entity_labels_cover_bare_and_empty_surfaces() {
        // `None` and `""` label the bare relation; the quoted surface form
        // is pinned by `test_source_link_label_without_cached_span`.
        fn entity(surface: Option<String>) -> BlockEntityLink {
            BlockEntityLink {
                block_id: uuid(BLOCK_ID),
                entity_id: Uuid::new_v4(),
                relation: "renders".to_owned(),
                surface_form: surface,
                created_at: Utc::now(),
            }
        }
        let mut view = assembled();
        view.blocks[0].links = Some(BlockLinks {
            sources: Vec::new(),
            entities: vec![entity(None), entity(Some(String::new()))],
        });
        let node = block_down(&view.blocks[0].clone(), &view);
        let labels: Vec<&str> = node
            .children
            .iter()
            .filter(|child| child.kind == "entity_link")
            .map(|child| child.label.as_str())
            .collect();
        assert_eq!(labels, vec!["renders", "renders"]);
    }

    #[test]
    fn test_block_down_reaches_child_blocks() {
        let mut view = assembled();
        let mut child = work_block();
        child.id = Uuid::new_v4();
        child.block_key = Uuid::new_v4();
        child.parent_id = Some(uuid(BLOCK_ID));
        let key = child.block_key.to_string();
        view.blocks.push(AssembledBlock {
            block: child,
            parent_key: Some(uuid(BLOCK_KEY)),
            citations: Vec::new(),
            links: Some(BlockLinks::default()),
        });
        let root = view.blocks[0].clone();
        let node = block_down(&root, &view);
        assert!(node
            .children
            .iter()
            .any(|child| child.kind == "block" && child.id == Some(key.clone())));
    }

    #[test]
    fn test_trace_work_without_a_current_revision_is_not_found() {
        let mut rig = Fakes::with_work();
        rig.works[0].current_revision_id = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut sel = selector();
        sel.slug = Some("deror".to_owned());
        let error = block_on(service.trace(sel)).expect_err("no current");
        assert!(
            matches!(error, Error::NotFound { kind: "work_revision", id } if id == "current of deror")
        );
    }

    #[test]
    fn test_trace_work_with_a_missing_revision_is_not_found() {
        let mut rig = Fakes::with_work();
        rig.revisions.remove(&uuid(REV_ID));
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut sel = selector();
        sel.slug = Some("deror".to_owned());
        let error = block_on(service.trace(sel)).expect_err("missing revision");
        assert!(matches!(
            error,
            Error::NotFound {
                kind: "work_revision",
                ..
            }
        ));
    }

    fn multi_work_rig() -> Fakes {
        // Two unresolvable works ahead of the good one: no current revision,
        // then a dangling pointer. Every fan-out skip fires before the hit.
        let base = Fakes::with_work();
        let mut works = vec![
            Work {
                slug: "alpha".to_owned(),
                current_revision_id: None,
                ..base.works[0].clone()
            },
            Work {
                slug: "beta".to_owned(),
                current_revision_id: Some(Uuid::new_v4()),
                ..base.works[0].clone()
            },
        ];
        works.push(base.works[0].clone());
        Fakes {
            works,
            revisions: base.revisions,
            blocks: base.blocks,
            entries: base.entries,
            spans: base.spans,
            documents: base.documents,
            links_for_span: base.links_for_span,
            fail_list: false,
            fail_slug: false,
            fail_revision_get: false,
            fail_work_get: false,
            fail_block_by_key: false,
            fail_blocks_get: false,
            fail_citation_by_key: false,
            fail_citing_span: false,
            fail_links_for_span: false,
            fail_spans_get: false,
            fail_spans_for_document: false,
            fail_documents_get: false,
            fail_tree: false,
        }
    }

    #[test]
    fn test_trace_citation_skips_unresolvable_works() {
        let fakes = Mutex::new(multi_work_rig());
        let service = service(&fakes);
        let mut sel = selector();
        sel.citation_key = Some(CITE_KEY.to_owned());
        let node = block_on(service.trace(sel)).expect("trace citation");
        assert_eq!(node.kind, "citation");
        assert_eq!(node.id.as_deref(), Some(CITE_KEY));

        let mut missing = selector();
        missing.citation_key = Some(Uuid::new_v4().to_string());
        let error = block_on(service.trace(missing)).expect_err("missing citation");
        assert!(matches!(
            error,
            Error::NotFound {
                kind: "citation_occurrence",
                ..
            }
        ));
    }

    #[test]
    fn test_trace_block_skips_unresolvable_works() {
        let mut rig = multi_work_rig();
        // A decoy block ahead of the match walks the assembled view past a
        // non-matching row before the hit.
        let mut decoy = work_block();
        decoy.id = Uuid::new_v4();
        decoy.block_key = Uuid::new_v4();
        rig.blocks.insert(0, decoy);
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut sel = selector();
        sel.block_key = Some(BLOCK_KEY.to_owned());
        let node = block_on(service.trace(sel)).expect("trace block");
        assert_eq!(node.kind, "block");
        assert_eq!(node.id.as_deref(), Some(BLOCK_KEY));
    }

    #[test]
    fn test_trace_span_and_document_misses_are_not_found() {
        let fakes = Mutex::new(Fakes::with_work());
        let service = service(&fakes);
        let mut span = selector();
        span.source_span_id = Some(Uuid::new_v4().to_string());
        let error = block_on(service.trace(span)).expect_err("missing span");
        assert!(matches!(
            error,
            Error::NotFound {
                kind: "source_span",
                ..
            }
        ));

        let mut document = selector();
        document.document_id = Some(Uuid::new_v4().to_string());
        let error = block_on(service.trace(document)).expect_err("missing document");
        assert!(matches!(
            error,
            Error::NotFound {
                kind: "document",
                ..
            }
        ));

        let mut bad = selector();
        bad.document_id = Some("nope".to_owned());
        let error = block_on(service.trace(bad)).expect_err("bad uuid");
        assert!(
            matches!(error, Error::Validation(message) if message == "document_id is not a UUID: nope")
        );
    }

    #[test]
    fn test_trace_span_without_a_document_row_names_offsets() {
        // The span's document row is a courtesy join: without it the span
        // still traces under its bare offsets.
        let mut rig = Fakes::with_work();
        rig.documents.remove(&uuid(DOC_ID));
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut sel = selector();
        sel.source_span_id = Some(SPAN_ID.to_owned());
        let node = block_on(service.trace(sel)).expect("trace span");
        assert_eq!(node.children[0].kind, "document");
        assert_eq!(
            node.children[0].label,
            format!("{DOC_ID} offsets 34\u{2013}62")
        );
    }

    #[test]
    fn test_span_up_citation_without_a_resolvable_work_names_only_block() {
        // The citing block's revision names no work row, so the label stops
        // at the block instead of naming its work.
        let mut rig = Fakes::with_work();
        rig.revisions.remove(&uuid(REV_ID));
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut sel = selector();
        sel.source_span_id = Some(SPAN_ID.to_owned());
        let node = block_on(service.trace(sel)).expect("trace span");
        let citing = node
            .children
            .iter()
            .find(|child| child.kind == "citation")
            .expect("citing occurrence");
        assert_eq!(
            citing.label,
            format!("{CITE_KEY} in block {BLOCK_KEY} (paragraph)")
        );
    }

    #[test]
    fn test_span_up_citation_without_a_block_names_only_the_key() {
        let mut rig = Fakes::with_work();
        rig.blocks.clear();
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut sel = selector();
        sel.source_span_id = Some(SPAN_ID.to_owned());
        let node = block_on(service.trace(sel)).expect("trace span");
        let citing = node
            .children
            .iter()
            .find(|child| child.kind == "citation")
            .expect("citing occurrence");
        assert_eq!(citing.label, CITE_KEY);
    }

    #[test]
    fn test_span_up_source_links_name_blocks_or_bare_relations() {
        let mut rig = Fakes::with_work();
        rig.links_for_span = vec![
            BlockSourceLink {
                block_id: uuid(BLOCK_ID),
                source_span_id: uuid(SPAN_ID),
                relation: "discusses".to_owned(),
                confidence: None,
                note: None,
                created_at: Utc::now(),
            },
            BlockSourceLink {
                block_id: Uuid::new_v4(),
                source_span_id: uuid(SPAN_ID),
                relation: "mentions".to_owned(),
                confidence: None,
                note: None,
                created_at: Utc::now(),
            },
        ];
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut sel = selector();
        sel.source_span_id = Some(SPAN_ID.to_owned());
        let node = block_on(service.trace(sel)).expect("trace span");
        let links: Vec<&str> = node
            .children
            .iter()
            .filter(|child| child.kind == "source_link")
            .map(|child| child.label.as_str())
            .collect();
        let discusses = format!("discusses from block {BLOCK_KEY} (paragraph)");
        assert_eq!(links, vec![discusses.as_str(), "mentions"]);
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
        use marginalia_types::documents::{DocumentDraft, DocumentFilter};
        use marginalia_types::works::{
            BlockEntityLinkDraft, BlockSourceLinkDraft, WorkBlockDraft, WorkDraft,
            WorkRevisionDraft,
        };
        let fakes = Mutex::new(Fakes::with_work());
        let shared = Shared(&fakes);
        let mut unit = ();
        {
            let error = block_on(WorkRepo::insert(
                &shared,
                &mut unit,
                WorkDraft {
                    slug: "deror".to_owned(),
                    title: "Deror".to_owned(),
                    work_type: "essay".to_owned(),
                    language: None,
                    abstract_text: None,
                    metadata: serde_json::Map::new(),
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        block_on(shared.set_current_revision(&mut unit, uuid(WORK_ID), uuid(REV_ID)))
            .expect("set current");
        {
            let error = block_on(shared.update(
                &mut unit,
                uuid(WORK_ID),
                Utc::now(),
                serde_json::Map::new(),
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.archive(&mut unit, uuid(WORK_ID)))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        let mut rev_tx = ();
        {
            let error = block_on(WorkRevisionRepo::insert(
                &shared,
                &mut rev_tx,
                WorkRevisionDraft {
                    work_id: uuid(WORK_ID),
                    revision_number: 1,
                    parent_revision_id: None,
                    message: None,
                    created_by: "user".to_owned(),
                    metadata: serde_json::Map::new(),
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(shared.latest(uuid(WORK_ID)))
            .expect("latest")
            .is_none());
        {
            let error = block_on(shared.copy_forward(&mut rev_tx, uuid(REV_ID)))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.set_message(&mut rev_tx, uuid(REV_ID), "note"))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.freeze(&mut rev_tx, uuid(REV_ID), b"hash"))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.publish(&mut rev_tx, uuid(REV_ID)))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.supersede(&mut rev_tx, uuid(REV_ID)))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        let block_draft = WorkBlockDraft {
            revision_id: uuid(REV_ID),
            block_key: Uuid::new_v4(),
            parent_id: None,
            position: 0,
            block_type: "paragraph".to_owned(),
            title: None,
            body_markdown: "words".to_owned(),
            attributes: serde_json::Map::new(),
        };
        {
            let error = block_on(shared.upsert(&mut unit, uuid(REV_ID), block_draft, None))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(
            block_on(shared.by_key_in_tx(&mut unit, uuid(REV_ID), uuid(BLOCK_KEY)))
                .expect("by_key_in_tx")
                .is_none()
        );
        block_on(WorkBlockRepo::delete(&shared, &mut unit, uuid(BLOCK_ID))).expect("delete");
        {
            let error = block_on(shared.insert_occurrence(
                &mut rev_tx,
                OccurrenceDraft {
                    block_id: uuid(BLOCK_ID),
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
            let error = block_on(shared.insert_item(
                &mut rev_tx,
                CitationItemDraft {
                    occurrence_id: Uuid::new_v4(),
                    position: 0,
                    edition_id: None,
                    edition_key: Some("DABAR_2026".to_owned()),
                    source_span_id: None,
                    quoted_text: None,
                    verify_status: None,
                    locator: serde_json::Map::new(),
                    prefix: None,
                    suffix: None,
                    suppress_author: false,
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(CitationRepo::for_block(&shared, uuid(BLOCK_ID)))
            .expect("for_block")
            .is_empty());
        assert!(block_on(shared.citing_key("DABAR_2026"))
            .expect("citing_key")
            .is_empty());
        {
            let error = block_on(shared.add_source_link(
                &mut unit,
                BlockSourceLinkDraft {
                    block_id: uuid(BLOCK_ID),
                    source_span_id: uuid(SPAN_ID),
                    relation: "discusses".to_owned(),
                    confidence: None,
                    note: None,
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.add_entity_link(
                &mut unit,
                BlockEntityLinkDraft {
                    block_id: uuid(BLOCK_ID),
                    entity_id: Uuid::new_v4(),
                    relation: "renders".to_owned(),
                    surface_form: None,
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(shared.for_entity(Uuid::new_v4(), "renders"))
            .expect("for_entity")
            .is_empty());
        let mut span_tx = ();
        {
            let error = block_on(shared.resolve(&mut span_tx, uuid(DOC_ID), 0, 4))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(shared.stale(10)).expect("stale").is_empty());
        {
            let error = block_on(DocumentRepo::insert(
                &shared,
                &mut unit,
                DocumentDraft {
                    title: Some("Amos".to_owned()),
                    document_type: "scripture".to_owned(),
                    language: None,
                    source: "test://amos".to_owned(),
                    content_hash: vec![0],
                    parser: "test".to_owned(),
                    parser_version: "1".to_owned(),
                    created_date_start: None,
                    created_date_end: None,
                    created_precision: None,
                    edition_id: None,
                    metadata: serde_json::Map::new(),
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(shared.get_many(&[uuid(DOC_ID)]))
            .expect("get_many")
            .is_empty());
        assert!(block_on(shared.find_by_hash(&[0], "test://amos"))
            .expect("find_by_hash")
            .is_none());
        {
            let error = block_on(shared.update_metadata(uuid(DOC_ID), serde_json::Map::new()))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(shared.iter_by_filter(&DocumentFilter::default()))
            .expect("iter_by_filter")
            .is_empty());
        assert_eq!(block_on(shared.count(None)).expect("count"), 0);
        block_on(DocumentRepo::delete(&shared, uuid(DOC_ID))).expect("delete");
    }

    #[test]
    fn test_infrastructure_failures_surface_as_storage_errors() {
        // Every repository read reports storage errors upward: the trace
        // names the failing read. Each case names the rig flag, the
        // selector entering the path, and the message.
        fn rigged(flag: fn(&mut Fakes)) -> Mutex<Fakes> {
            let mut rig = Fakes::with_work();
            flag(&mut rig);
            Mutex::new(rig)
        }
        fn select(
            slug: Option<&str>,
            block: Option<&str>,
            citation: Option<&str>,
            span: Option<&str>,
            document: Option<&str>,
        ) -> TraceSelector {
            let mut sel = selector();
            sel.slug = slug.map(str::to_owned);
            sel.block_key = block.map(str::to_owned);
            sel.citation_key = citation.map(str::to_owned);
            sel.source_span_id = span.map(str::to_owned);
            sel.document_id = document.map(str::to_owned);
            sel
        }
        let work = || select(Some("deror"), None, None, None, None);
        let citation = || select(None, None, Some(CITE_KEY), None, None);
        let block = || select(None, Some(BLOCK_KEY), None, None, None);
        let span = || select(None, None, None, Some(SPAN_ID), None);
        let document = || select(None, None, None, None, Some(DOC_ID));
        type FailCase = (fn(&mut Fakes), fn() -> TraceSelector, &'static str);
        let cases: [FailCase; 19] = [
            (|rig| rig.fail_slug = true, work, "works failed"),
            (|rig| rig.fail_revision_get = true, work, "revisions failed"),
            (|rig| rig.fail_tree = true, work, "tree failed"),
            (|rig| rig.fail_list = true, citation, "list failed"),
            (
                |rig| rig.fail_revision_get = true,
                citation,
                "revisions failed",
            ),
            (
                |rig| rig.fail_citation_by_key = true,
                citation,
                "citations failed",
            ),
            (|rig| rig.fail_tree = true, citation, "tree failed"),
            (|rig| rig.fail_list = true, block, "list failed"),
            (
                |rig| rig.fail_revision_get = true,
                block,
                "revisions failed",
            ),
            (|rig| rig.fail_block_by_key = true, block, "blocks failed"),
            (|rig| rig.fail_tree = true, block, "tree failed"),
            (|rig| rig.fail_spans_get = true, span, "spans failed"),
            (|rig| rig.fail_citing_span = true, span, "citations failed"),
            (|rig| rig.fail_links_for_span = true, span, "links failed"),
            (|rig| rig.fail_blocks_get = true, span, "blocks failed"),
            (|rig| rig.fail_revision_get = true, span, "revisions failed"),
            (|rig| rig.fail_work_get = true, span, "work failed"),
            (
                |rig| rig.fail_documents_get = true,
                document,
                "documents failed",
            ),
            (
                |rig| rig.fail_spans_for_document = true,
                document,
                "spans failed",
            ),
        ];
        for (flag, setup, message) in cases {
            let fakes = rigged(flag);
            let service = service(&fakes);
            let error = block_on(service.trace(setup())).expect_err("infra fails");
            assert!(
                matches!(error, Error::Storage(failure) if failure == message),
                "wrong failure for {message}"
            );
        }
    }

    #[test]
    fn test_document_span_up_failure_aborts_the_document_trace() {
        // A document trace fans out per span: a failing up-leg for one of
        // its spans aborts the whole trace.
        let mut rig = Fakes::with_work();
        rig.fail_citing_span = true;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut sel = selector();
        sel.document_id = Some(DOC_ID.to_owned());
        let error = block_on(service.trace(sel)).expect_err("span_up fails");
        assert!(matches!(error, Error::Storage(message) if message == "citations failed"));
    }

    #[test]
    fn test_occurrence_down_skips_spans_missing_from_the_cache() {
        // An item naming a span id with no cached row hangs nothing under
        // its occurrence, exactly like a spanless item.
        let mut missing = item_with_span();
        missing.source_span_id = Some(Uuid::new_v4());
        let view = assembled();
        let node = occurrence_down(
            &view,
            &BlockCitations {
                occurrence: occurrence(),
                items: vec![missing],
            },
        );
        assert_eq!(node.kind, "citation");
        assert!(node.children.is_empty());
    }

    #[test]
    fn test_titleless_document_traces_under_its_bare_id() {
        // The document label prefers the title but falls back to the bare
        // id, mirroring the span-down courtesy join.
        let mut rig = Fakes::with_work();
        rig.documents
            .get_mut(&uuid(DOC_ID))
            .expect("document")
            .title = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut sel = selector();
        sel.document_id = Some(DOC_ID.to_owned());
        let node = block_on(service.trace(sel)).expect("trace document");
        assert_eq!(node.kind, "document");
        assert_eq!(node.label, DOC_ID);
    }

    #[test]
    fn test_bad_block_and_citation_uuids_name_their_fields() {
        let fakes = Mutex::new(Fakes::with_work());
        let service = service(&fakes);
        let mut block = selector();
        block.block_key = Some("nope".to_owned());
        let error = block_on(service.trace(block)).expect_err("bad uuid");
        assert!(
            matches!(error, Error::Validation(message) if message == "block_key is not a UUID: nope")
        );
        let mut citation = selector();
        citation.citation_key = Some("nope".to_owned());
        let error = block_on(service.trace(citation)).expect_err("bad uuid");
        assert!(
            matches!(error, Error::Validation(message) if message == "citation_key is not a UUID: nope")
        );
    }

    #[test]
    fn test_span_down_names_a_titleless_document_row() {
        // A present document row without a title still labels the span:
        // the courtesy join falls back to the bare document id.
        let mut rig = Fakes::with_work();
        rig.documents
            .get_mut(&uuid(DOC_ID))
            .expect("document")
            .title = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut sel = selector();
        sel.source_span_id = Some(SPAN_ID.to_owned());
        let node = block_on(service.trace(sel)).expect("trace span");
        assert_eq!(
            node.children[0].label,
            format!("{DOC_ID} offsets 34\u{2013}62")
        );
    }

    #[test]
    fn test_span_up_link_failure_without_citations_aborts() {
        // With no citing occurrences, the source-link leg is reached: a
        // failing block read there aborts the span trace.
        let mut rig = Fakes::with_work();
        rig.entries.clear();
        rig.links_for_span = vec![BlockSourceLink {
            block_id: uuid(BLOCK_ID),
            source_span_id: uuid(SPAN_ID),
            relation: "discusses".to_owned(),
            confidence: None,
            note: None,
            created_at: Utc::now(),
        }];
        rig.fail_blocks_get = true;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut sel = selector();
        sel.source_span_id = Some(SPAN_ID.to_owned());
        let error = block_on(service.trace(sel)).expect_err("link block fails");
        assert!(matches!(error, Error::Storage(message) if message == "blocks failed"));
    }
}
