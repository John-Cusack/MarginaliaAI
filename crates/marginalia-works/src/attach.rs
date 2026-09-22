//! Attach a citation to a block — the Phase-1 `work_cite` write boundary.
//!
//! Python source: `services/works/attach.py` (`CitationService`). One
//! transaction: resolve the block in the work's current draft revision,
//! resolve the bibliographic identity, verify the quote when one is given,
//! apply the narrowing rule for the intent, resolve the span, and insert the
//! occurrence plus its item. Any refusal writes nothing, and the refusal
//! carries its rule id.
//!
//! `near` quotes are stored, not refused; `not_found` and
//! `no_canonical_text` store nothing, because there is no address to store.

use std::fmt;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use marginalia_types::citations::{CitationItemDraft, OccurrenceDraft};
use marginalia_types::ports::{
    CitationRepo, DocumentRepo, EditionRepo, PassageRepo, SourceSpanRepo, WorkBlockRepo, WorkRepo,
    WorkRevisionRepo,
};
use marginalia_types::works::{Placement, RevisionState};
use marginalia_types::works_files::Intent;
use marginalia_types::works_ports::{TxFactory, VerifyPort, VerifyTier};
use marginalia_types::{Error, Result};

use crate::markers::format_marker;
use crate::verify::MAX_QUOTE_CHARS;
use marginalia_text::repr::py_repr_str;

/// The citation was refused with nothing written; the tool reports the rule.
#[derive(Debug, Clone, PartialEq)]
pub struct AttachRefused {
    pub rule_id: String,
    pub message: String,
    pub detail: Option<Map<String, Value>>,
}

impl fmt::Display for AttachRefused {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for AttachRefused {}

/// Attach failure: a rule refusal, or an infrastructure error (mirroring
/// `ValueError`, `NotFoundError`, and `FrozenRevisionError`).
#[derive(Debug)]
pub enum AttachError {
    Refused(AttachRefused),
    Failed(Error),
}

impl fmt::Display for AttachError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Refused(refused) => write!(formatter, "{refused}"),
            Self::Failed(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for AttachError {}

impl From<Error> for AttachError {
    fn from(error: Error) -> Self {
        Self::Failed(error)
    }
}

pub type AttachResult<T> = std::result::Result<T, AttachError>;

/// Intents that must cite a narrowed span, never a whole passage.
pub fn is_narrow_intent(intent: marginalia_types::works_files::Intent) -> bool {
    matches!(intent, Intent::Quotation | Intent::Translation)
}

/// Intents warned when they cite a whole passage.
pub fn is_region_warn_intent(intent: marginalia_types::works_files::Intent) -> bool {
    matches!(
        intent,
        Intent::Support | Intent::Source | Intent::Definition
    )
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttachedItem {
    #[serde(default)]
    pub source_span_id: Option<Uuid>,
    #[serde(default)]
    pub char_start: Option<i64>,
    #[serde(default)]
    pub char_end: Option<i64>,
    #[serde(default)]
    pub verify_status: Option<String>,
    #[serde(default)]
    pub edition_id: Option<Uuid>,
    #[serde(default)]
    pub edition_key: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CitationAttached {
    pub occurrence_id: Uuid,
    pub citation_key: Uuid,
    pub marker: String,
    pub item: AttachedItem,
    #[serde(default)]
    pub warnings: Vec<String>,
}

/// All `attach` inputs; `placement` defaults to `"inline"` at construction.
#[derive(Debug, Clone, PartialEq)]
pub struct AttachRequest {
    pub slug: String,
    pub block_key: Uuid,
    pub intent: String,
    pub quote: Option<String>,
    pub document_id: Option<Uuid>,
    pub window: Option<(i64, i64)>,
    pub edition_key: Option<String>,
    pub edition_id: Option<Uuid>,
    pub locator: Option<Map<String, Value>>,
    pub prefix: Option<String>,
    pub suffix: Option<String>,
    pub placement: String,
    pub citation_key: Option<Uuid>,
}

impl AttachRequest {
    pub fn new(slug: String, block_key: Uuid, intent: String) -> Self {
        Self {
            slug,
            block_key,
            intent,
            quote: None,
            document_id: None,
            window: None,
            edition_key: None,
            edition_id: None,
            locator: None,
            prefix: None,
            suffix: None,
            placement: "inline".to_owned(),
            citation_key: None,
        }
    }
}

fn parse_intent(raw: &str) -> AttachResult<Intent> {
    match raw {
        "quotation" => Ok(Intent::Quotation),
        "translation" => Ok(Intent::Translation),
        "support" => Ok(Intent::Support),
        "contrast" => Ok(Intent::Contrast),
        "background" => Ok(Intent::Background),
        "definition" => Ok(Intent::Definition),
        "source" => Ok(Intent::Source),
        "see_also" => Ok(Intent::SeeAlso),
        _ => Err(AttachError::Failed(Error::Validation(format!(
            "Unknown intent {}",
            py_repr_str(raw)
        )))),
    }
}

fn parse_placement(raw: &str) -> AttachResult<Placement> {
    match raw {
        "inline" => Ok(Placement::Inline),
        "block_end" => Ok(Placement::BlockEnd),
        _ => Err(AttachError::Failed(Error::Validation(format!(
            "Unknown placement {}",
            py_repr_str(raw)
        )))),
    }
}

fn revision_state_value(state: RevisionState) -> &'static str {
    match state {
        RevisionState::Draft => "draft",
        RevisionState::Frozen => "frozen",
        RevisionState::Published => "published",
        RevisionState::Superseded => "superseded",
    }
}

/// Verify-then-write for citation rows.
pub struct CitationService<V, S, E, C, W, R, B, P, D, Tx, F> {
    verification: V,
    spans: S,
    editions: E,
    citations: C,
    works: W,
    revisions: R,
    blocks: B,
    passages: P,
    documents: D,
    tx_factory: F,
    tx_marker: std::marker::PhantomData<Tx>,
}

impl<V, S, E, C, W, R, B, P, D, Tx, F> CitationService<V, S, E, C, W, R, B, P, D, Tx, F>
where
    V: VerifyPort,
    S: SourceSpanRepo<Tx = Tx>,
    E: EditionRepo,
    C: CitationRepo<Tx = Tx>,
    W: WorkRepo,
    R: WorkRevisionRepo,
    B: WorkBlockRepo,
    P: PassageRepo,
    D: DocumentRepo,
    F: TxFactory<Tx = Tx>,
{
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        verification: V,
        spans: S,
        editions: E,
        citations: C,
        works: W,
        revisions: R,
        blocks: B,
        passages: P,
        documents: D,
        tx_factory: F,
    ) -> Self {
        Self {
            verification,
            spans,
            editions,
            citations,
            works,
            revisions,
            blocks,
            passages,
            documents,
            tx_factory,
            tx_marker: std::marker::PhantomData,
        }
    }

    /// Ground a block's marker in the corpus, or refuse with a rule id.
    pub async fn attach(&self, request: AttachRequest) -> AttachResult<CitationAttached> {
        let parsed_intent = parse_intent(&request.intent)?;
        let parsed_placement = parse_placement(&request.placement)?;

        let work = self
            .works
            .get_by_slug(&request.slug)
            .await
            .map_err(AttachError::Failed)?
            .ok_or_else(|| {
                AttachError::Failed(Error::NotFound {
                    kind: "work",
                    id: request.slug.clone(),
                })
            })?;
        let current_id = work.current_revision_id.ok_or_else(|| {
            AttachError::Failed(Error::NotFound {
                kind: "work_revision",
                id: format!("current of {}", request.slug),
            })
        })?;
        let revision = self
            .revisions
            .get(current_id)
            .await
            .map_err(AttachError::Failed)?
            // proof: the work row's foreign key keeps this pointer whole.
            .ok_or_else(|| {
                AttachError::Failed(Error::NotFound {
                    kind: "work_revision",
                    id: current_id.to_string(),
                })
            })?;
        let state = revision_state_value(revision.state);
        if state != "draft" {
            return Err(AttachError::Failed(Error::FrozenRevision(format!(
                "Revision {} is {state}, not draft: copy it forward to edit.",
                revision.id
            ))));
        }
        let block = self
            .blocks
            .by_key(revision.id, request.block_key)
            .await
            .map_err(AttachError::Failed)?
            .ok_or_else(|| {
                AttachError::Failed(Error::NotFound {
                    kind: "work_block",
                    id: request.block_key.to_string(),
                })
            })?;

        // Explicit identity wins and is never second-guessed: it is what the
        // mismatch check tests the resolved span against. When neither is
        // given, the edition is inherited from the span's document below.
        let mut resolved_edition_id = request.edition_id;
        if resolved_edition_id.is_none() {
            if let Some(key) = &request.edition_key {
                let edition = self
                    .editions
                    .get_by_key(key)
                    .await
                    .map_err(AttachError::Failed)?;
                if let Some(edition) = edition {
                    resolved_edition_id = Some(edition.id);
                }
            }
        }

        let mut tier: Option<String> = None;
        let mut span_document_id: Option<Uuid> = None;
        let mut char_start: Option<i64> = None;
        let mut char_end: Option<i64> = None;
        let mut warnings: Vec<String> = Vec::new();
        if let Some(quote) = &request.quote {
            let verified = self
                .verification
                .verify(quote, request.document_id, request.window)
                .await
                .map_err(AttachError::Failed)?;
            if verified.tier == VerifyTier::NoCanonicalText {
                let rendered = request
                    .document_id
                    .map_or("None".to_owned(), |id| id.to_string());
                return Err(AttachError::Refused(AttachRefused {
                    rule_id: "AUTH_SOURCE_UNCHECKABLE".to_owned(),
                    message: format!(
                        "Document {rendered} has no canonical text, so the quote cannot be checked. Nothing was written."
                    ),
                    detail: Some(Map::from_iter([(
                        "tier".to_owned(),
                        Value::String(verified.tier.as_str().to_owned()),
                    )])),
                }));
            }
            if verified.tier == VerifyTier::NotFound {
                return Err(AttachError::Refused(AttachRefused {
                    rule_id: "AUTH_QUOTE_UNVERIFIED".to_owned(),
                    message: "Quote verifies not_found: only exact, normalized, or near can be cited. Nothing was written."
                        .to_owned(),
                    detail: Some(Map::from_iter([
                        (
                            "tier".to_owned(),
                            Value::String(verified.tier.as_str().to_owned()),
                        ),
                        (
                            "divergence".to_owned(),
                            verified.divergence.as_ref().map_or(Value::Null, |divergence| {
                                Value::Object(Map::from_iter([
                                    (
                                        "matched_characters".to_owned(),
                                        Value::from(divergence.matched_characters),
                                    ),
                                    (
                                        "matched_tail".to_owned(),
                                        Value::String(divergence.matched_tail.clone()),
                                    ),
                                    (
                                        "quote_continues".to_owned(),
                                        Value::String(divergence.quote_continues.clone()),
                                    ),
                                    (
                                        "source_continues".to_owned(),
                                        Value::String(divergence.source_continues.clone()),
                                    ),
                                ]))
                            }),
                        ),
                    ])),
                }));
            }
            let Some(location) = &verified.location else {
                // A near miss whose prefix will not locate names no address:
                // nothing to resolve, so the refusal is the whole record.
                return Err(AttachError::Refused(AttachRefused {
                    rule_id: "AUTH_QUOTE_UNVERIFIED".to_owned(),
                    message: "Quote verifies near but the matching part will not locate: only a located span can be cited. Nothing was written."
                        .to_owned(),
                    detail: Some(Map::from_iter([(
                        "tier".to_owned(),
                        Value::String(verified.tier.as_str().to_owned()),
                    )])),
                }));
            };
            tier = Some(verified.tier.as_str().to_owned());
            span_document_id = Some(location.document_id);
            char_start = Some(location.char_start);
            char_end = Some(location.char_end);
            if is_narrow_intent(parsed_intent)
                && (self
                    .is_region(location.document_id, location.char_start, location.char_end)
                    .await
                    .map_err(AttachError::Failed)?
                    || location.char_end - location.char_start > MAX_QUOTE_CHARS)
            {
                return Err(AttachError::Refused(AttachRefused {
                    rule_id: "AUTH_SPAN_NOT_NARROWED".to_owned(),
                    message: "A quotation or translation must cite a narrowed span, not a whole passage. Nothing was written."
                        .to_owned(),
                    detail: None,
                }));
            }
            if is_region_warn_intent(parsed_intent)
                && self
                    .is_region(location.document_id, location.char_start, location.char_end)
                    .await
                    .map_err(AttachError::Failed)?
            {
                warnings.push("AUTH_SPAN_REGION".to_owned());
            }
        }

        let mut edition_key = request.edition_key.clone();
        if resolved_edition_id.is_none() && edition_key.is_none() {
            match self
                .inherit_edition(span_document_id)
                .await
                .map_err(AttachError::Failed)?
            {
                Some((key, id)) => {
                    edition_key = Some(key);
                    resolved_edition_id = id;
                }
                None => {
                    return Err(AttachError::Refused(AttachRefused {
                        rule_id: "AUTH_CITATION_EDITION_MISSING".to_owned(),
                        message: "No edition was given and the cited document names none: pass edition_key or edition_id, or key the document first. Nothing was written."
                            .to_owned(),
                        detail: None,
                    }));
                }
            }
        }

        // The fresh key is generated in-process like Python's uuid7().
        let key = request.citation_key.unwrap_or_else(Uuid::now_v7);
        let mut tx = self.tx_factory.begin().await.map_err(AttachError::Failed)?;
        // proof: the quote branch above sets the document and both bounds
        // together, or refuses before any write.
        let span_id = if request.quote.is_some() {
            let document_id = span_document_id.expect("verified quote names its document");
            let start = char_start.expect("verified quote names its start");
            let end = char_end.expect("verified quote names its end");
            match self.spans.resolve(&mut tx, document_id, start, end).await {
                Ok(span) => Some(span.id),
                Err(error) => {
                    let _ = self.tx_factory.rollback(tx).await;
                    return Err(AttachError::Failed(error));
                }
            }
        } else {
            None
        };
        let occurrence = match self
            .citations
            .insert_occurrence(
                &mut tx,
                OccurrenceDraft {
                    block_id: block.id,
                    citation_key: key,
                    placement: parsed_placement,
                    intent: parsed_intent,
                    note: None,
                },
            )
            .await
        {
            Ok(occurrence) => occurrence,
            Err(error) => {
                let _ = self.tx_factory.rollback(tx).await;
                return Err(AttachError::Failed(error));
            }
        };
        if let Err(error) = self
            .citations
            .insert_item(
                &mut tx,
                CitationItemDraft {
                    occurrence_id: occurrence.id,
                    position: 0,
                    edition_id: resolved_edition_id,
                    edition_key: edition_key.clone(),
                    source_span_id: span_id,
                    quoted_text: request.quote.clone(),
                    verify_status: tier.clone(),
                    locator: request.locator.clone().unwrap_or_default(),
                    prefix: request.prefix.clone(),
                    suffix: request.suffix.clone(),
                    suppress_author: false,
                },
            )
            .await
        {
            let _ = self.tx_factory.rollback(tx).await;
            return Err(AttachError::Failed(error));
        }
        self.tx_factory
            .commit(tx)
            .await
            .map_err(AttachError::Failed)?;
        Ok(CitationAttached {
            occurrence_id: occurrence.id,
            citation_key: key,
            marker: format_marker(&key),
            item: AttachedItem {
                source_span_id: span_id,
                char_start,
                char_end,
                verify_status: tier,
                edition_id: resolved_edition_id,
                edition_key,
            },
            warnings,
        })
    }

    /// The span's document key, when the caller named no edition.
    ///
    /// Returns the document's key with its edition id when one exists, so a
    /// quote against a keyed document cites without repeating the obvious.
    /// `None` means there is nothing to inherit — a spanless cite, or a
    /// document nobody keyed — and the caller must name the edition.
    async fn inherit_edition(
        &self,
        span_document_id: Option<Uuid>,
    ) -> Result<Option<(String, Option<Uuid>)>> {
        let Some(document_id) = span_document_id else {
            return Ok(None);
        };
        let document = self.documents.get(document_id).await?;
        // proof: spans RESTRICT documents, so the row is always there.
        let Some(document) = document else {
            return Ok(None);
        };
        let key = match document.metadata.get("edition_key") {
            Some(Value::String(key)) if !key.is_empty() => key.clone(),
            _ => return Ok(None),
        };
        let edition = self.editions.get_by_key(&key).await?;
        Ok(Some((key, edition.map(|edition| edition.id))))
    }

    /// Whether this span coincides with one passage row's bounds.
    async fn is_region(&self, document_id: Uuid, char_start: i64, char_end: i64) -> Result<bool> {
        let covering = self
            .passages
            .covering_span(document_id, char_start, char_end)
            .await?;
        Ok(covering.iter().any(|passage| {
            passage.char_start == Some(char_start) && passage.char_end == Some(char_end)
        }))
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
    use marginalia_types::citations::{BlockCitations, CitationItem, CitationOccurrence};
    use marginalia_types::documents::Document;
    use marginalia_types::passages::Passage;
    use marginalia_types::spans::SourceSpan;
    use marginalia_types::works::{Edition, Work, WorkBlock, WorkRevision, WorkStatus};
    use marginalia_types::works_ports::{VerifyDivergence, VerifyLocation, VerifyResult};

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
    const DOC_ID: &str = "cccccccc-cccc-cccc-cccc-cccccccccccc";
    const EDITION_ID: &str = "dddddddd-dddd-dddd-dddd-dddddddddddd";

    fn uuid(raw: &str) -> Uuid {
        Uuid::parse_str(raw).expect("fixture uuid")
    }

    #[derive(Debug)]
    struct FakeTx;

    struct Fakes {
        work: Option<Work>,
        revision: Option<WorkRevision>,
        block: Option<WorkBlock>,
        verify: VerifyResult,
        covering: Vec<Passage>,
        document: Option<Document>,
        editions: HashMap<String, Edition>,
        span: SourceSpan,
        inserted_items: Vec<CitationItemDraft>,
        inserted_edition_ids: Vec<Option<Uuid>>,
        fail_works: bool,
        fail_revisions: bool,
        fail_blocks: bool,
        fail_editions: bool,
        fail_verify: bool,
        fail_covering: bool,
        fail_documents: bool,
        fail_begin: bool,
        fail_commit: bool,
        recorded: Vec<(Intent, Placement)>,
        fail_resolve: bool,
        fail_occurrence: bool,
        fail_item: bool,
    }

    impl Fakes {
        fn rig() -> Self {
            let now = Utc::now();
            let mut metadata = Map::new();
            metadata.insert(
                "edition_key".to_owned(),
                Value::String("DABAR_2026".to_owned()),
            );
            Self {
                work: Some(Work {
                    id: uuid(WORK_ID),
                    slug: "deror".to_owned(),
                    title: "Deror".to_owned(),
                    work_type: "essay".to_owned(),
                    status: WorkStatus::Draft,
                    language: None,
                    abstract_text: None,
                    current_revision_id: Some(uuid(REV_ID)),
                    metadata: Map::new(),
                    created_at: now,
                    updated_at: now,
                    archived_at: None,
                }),
                revision: Some(WorkRevision {
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
                    metadata: Map::new(),
                }),
                block: Some(WorkBlock {
                    id: uuid(BLOCK_ID),
                    revision_id: uuid(REV_ID),
                    block_key: uuid(BLOCK_KEY),
                    parent_id: None,
                    position: 0,
                    block_type: "paragraph".to_owned(),
                    title: None,
                    body_markdown: "The prophets speak.".to_owned(),
                    attributes: Map::new(),
                    created_at: now,
                    updated_at: now,
                }),
                verify: VerifyResult {
                    tier: VerifyTier::Exact,
                    location: Some(VerifyLocation {
                        document_id: uuid(DOC_ID),
                        char_start: 34,
                        char_end: 62,
                    }),
                    matched_fraction: None,
                    divergence: None,
                },
                covering: Vec::new(),
                document: Some(Document {
                    id: uuid(DOC_ID),
                    title: Some("Amos".to_owned()),
                    document_type: "scripture".to_owned(),
                    language: None,
                    source: "test://amos".to_owned(),
                    content_hash: vec![0],
                    parser: "test".to_owned(),
                    parser_version: "1".to_owned(),
                    ingested_at: now,
                    created_date_start: None,
                    created_date_end: None,
                    created_precision: None,
                    edition_id: None,
                    metadata,
                }),
                editions: HashMap::from([(
                    "DABAR_2026".to_owned(),
                    Edition {
                        id: uuid(EDITION_ID),
                        edition_key: "DABAR_2026".to_owned(),
                        csl: Map::new(),
                        created_at: now,
                    },
                )]),
                span: SourceSpan {
                    id: Uuid::new_v4(),
                    document_id: uuid(DOC_ID),
                    char_start: 34,
                    char_end: 62,
                    quoted_text: "The prophets pair two words.".to_owned(),
                    parser: None,
                    parser_version: None,
                    passage_id: None,
                    created_at: now,
                },
                inserted_items: Vec::new(),
                inserted_edition_ids: Vec::new(),
                fail_works: false,
                fail_revisions: false,
                fail_blocks: false,
                fail_editions: false,
                fail_verify: false,
                fail_covering: false,
                fail_documents: false,
                fail_begin: false,
                fail_commit: false,
                recorded: Vec::new(),
                fail_resolve: false,
                fail_occurrence: false,
                fail_item: false,
            }
        }

        fn with_region(mut self) -> Self {
            self.covering = vec![Passage {
                id: Uuid::new_v4(),
                document_id: uuid(DOC_ID),
                position: 0,
                char_start: Some(34),
                char_end: Some(62),
                locator: Map::new(),
                text: "x".to_owned(),
                token_count: None,
                chunker: "test".to_owned(),
                chunker_version: "1".to_owned(),
                metadata: Map::new(),
                node_id: None,
                content_hash: vec![0],
                created_at: Utc::now(),
            }];
            self
        }
    }

    struct Shared<'a>(&'a Mutex<Fakes>);

    impl<'a> VerifyPort for Shared<'a> {
        async fn verify(
            &self,
            _quote: &str,
            _document_id: Option<Uuid>,
            _window: Option<(i64, i64)>,
        ) -> Result<VerifyResult> {
            let guard = self.0.lock().expect("lock");
            if guard.fail_verify {
                return Err(Error::Storage("verify failed".to_owned()));
            }
            Ok(guard.verify.clone())
        }
    }

    impl<'a> SourceSpanRepo for Shared<'a> {
        type Tx = FakeTx;
        async fn resolve(
            &self,
            _tx: &mut Self::Tx,
            document_id: Uuid,
            char_start: i64,
            char_end: i64,
        ) -> Result<SourceSpan> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_resolve {
                return Err(Error::Storage("resolve failed".to_owned()));
            }
            guard.span.document_id = document_id;
            guard.span.char_start = char_start;
            guard.span.char_end = char_end;
            Ok(guard.span.clone())
        }
        async fn get(&self, _span_id: Uuid) -> Result<Option<SourceSpan>> {
            Ok(None)
        }
        async fn for_document(&self, _document_id: Uuid) -> Result<Vec<SourceSpan>> {
            Ok(Vec::new())
        }
        async fn stale(&self, _limit: i64) -> Result<Vec<SourceSpan>> {
            Ok(Vec::new())
        }
    }

    impl<'a> EditionRepo for Shared<'a> {
        type Tx = ();
        async fn get(&self, _edition_id: Uuid) -> Result<Option<Edition>> {
            Ok(None)
        }
        async fn get_by_key(&self, edition_key: &str) -> Result<Option<Edition>> {
            let guard = self.0.lock().expect("lock");
            if guard.fail_editions {
                return Err(Error::Storage("editions failed".to_owned()));
            }
            Ok(guard.editions.get(edition_key).cloned())
        }
        async fn upsert_key(
            &self,
            _tx: &mut Self::Tx,
            _edition_key: &str,
            _csl: Option<Map<String, Value>>,
            _lock: bool,
        ) -> Result<Edition> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn list_keys(&self) -> Result<Vec<String>> {
            Ok(Vec::new())
        }
    }

    impl<'a> CitationRepo for Shared<'a> {
        type Tx = FakeTx;
        async fn insert_occurrence(
            &self,
            _tx: &mut Self::Tx,
            draft: OccurrenceDraft,
        ) -> Result<CitationOccurrence> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_occurrence {
                return Err(Error::Storage("occurrence failed".to_owned()));
            }
            guard.recorded.push((draft.intent, draft.placement));
            Ok(CitationOccurrence {
                id: Uuid::new_v4(),
                citation_key: draft.citation_key,
                block_id: draft.block_id,
                placement: draft.placement,
                intent: draft.intent,
                note: None,
                created_at: Utc::now(),
            })
        }
        async fn insert_item(
            &self,
            _tx: &mut Self::Tx,
            draft: CitationItemDraft,
        ) -> Result<CitationItem> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_item {
                return Err(Error::Storage("item failed".to_owned()));
            }
            guard.inserted_edition_ids.push(draft.edition_id);
            guard.inserted_items.push(draft.clone());
            Ok(CitationItem {
                occurrence_id: draft.occurrence_id,
                position: draft.position,
                edition_id: draft.edition_id,
                edition_key: draft.edition_key,
                source_span_id: draft.source_span_id,
                quoted_text: draft.quoted_text,
                verify_status: draft.verify_status,
                verified_at: None,
                locator: draft.locator,
                prefix: draft.prefix,
                suffix: draft.suffix,
                suppress_author: false,
            })
        }
        async fn for_block(&self, _block_id: Uuid) -> Result<Vec<BlockCitations>> {
            Ok(Vec::new())
        }
        async fn for_revision(&self, _revision_id: Uuid) -> Result<Vec<BlockCitations>> {
            Ok(Vec::new())
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

    impl<'a> WorkRepo for Shared<'a> {
        type Tx = ();
        async fn insert(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::works::WorkDraft,
        ) -> Result<Work> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn get(&self, _work_id: Uuid) -> Result<Option<Work>> {
            Ok(None)
        }
        async fn get_by_slug(&self, slug: &str) -> Result<Option<Work>> {
            let guard = self.0.lock().expect("lock");
            if guard.fail_works {
                return Err(Error::Storage("works failed".to_owned()));
            }
            Ok(guard.work.clone().filter(|work| work.slug == slug))
        }
        async fn list(&self) -> Result<Vec<Work>> {
            Ok(self
                .0
                .lock()
                .expect("lock")
                .work
                .clone()
                .into_iter()
                .collect())
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
            _fields: Map<String, Value>,
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
            let guard = self.0.lock().expect("lock");
            if guard.fail_revisions {
                return Err(Error::Storage("revisions failed".to_owned()));
            }
            Ok(guard
                .revision
                .clone()
                .filter(|revision| revision.id == revision_id))
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
            Ok(Vec::new())
        }
        async fn get(&self, _block_id: Uuid) -> Result<Option<WorkBlock>> {
            Ok(None)
        }
        async fn by_key(&self, _revision_id: Uuid, block_key: Uuid) -> Result<Option<WorkBlock>> {
            let guard = self.0.lock().expect("lock");
            if guard.fail_blocks {
                return Err(Error::Storage("blocks failed".to_owned()));
            }
            Ok(guard
                .block
                .clone()
                .filter(|block| block.block_key == block_key))
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

    impl<'a> PassageRepo for Shared<'a> {
        type Tx = ();
        async fn insert_many(
            &self,
            _tx: &mut Self::Tx,
            _document_id: Uuid,
            _drafts: Vec<marginalia_types::passages::PassageDraft>,
        ) -> Result<Vec<Passage>> {
            Ok(Vec::new())
        }
        async fn get(&self, _passage_id: Uuid) -> Result<Option<Passage>> {
            Ok(None)
        }
        async fn get_by_document(&self, _document_id: Uuid) -> Result<Vec<Passage>> {
            Ok(Vec::new())
        }
        async fn get_context(
            &self,
            _passage_id: Uuid,
            _before: i64,
            _after: i64,
        ) -> Result<(Vec<Passage>, Passage, Vec<Passage>)> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn vector_search(
            &self,
            _query_embedding: &[f64],
            _model: &str,
            _model_version: &str,
            _candidate_ids: Option<&[Uuid]>,
            _k: i64,
        ) -> Result<Vec<(Uuid, f64)>> {
            Ok(Vec::new())
        }
        async fn keyword_search(
            &self,
            _query: &str,
            _lang: Option<&str>,
            _candidate_ids: Option<&[Uuid]>,
            _k: i64,
        ) -> Result<Vec<(Uuid, f64)>> {
            Ok(Vec::new())
        }
        async fn store_embeddings(
            &self,
            _tx: &mut Self::Tx,
            _passage_ids: &[Uuid],
            _embeddings: &[Vec<f64>],
            _model: &str,
            _model_version: &str,
            _dim: i64,
        ) -> Result<()> {
            Ok(())
        }
        async fn index_fts(
            &self,
            _tx: &mut Self::Tx,
            _passage_ids: &[Uuid],
            _texts: &[String],
            _lang: &str,
        ) -> Result<()> {
            Ok(())
        }
        async fn get_embedding(
            &self,
            _passage_id: Uuid,
            _model: &str,
            _model_version: &str,
        ) -> Result<Option<Vec<f64>>> {
            Ok(None)
        }
        async fn filter_candidate_ids<F: marginalia_types::ports::FilterExtension>(
            &self,
            _filters: &Map<String, Value>,
            _filter_extensions: Option<&HashMap<String, F>>,
        ) -> Result<Vec<Uuid>> {
            Ok(Vec::new())
        }
        async fn covering_span(
            &self,
            _document_id: Uuid,
            _char_start: i64,
            _char_end: i64,
        ) -> Result<Vec<Passage>> {
            let guard = self.0.lock().expect("lock");
            if guard.fail_covering {
                return Err(Error::Storage("covering failed".to_owned()));
            }
            Ok(guard.covering.clone())
        }
        async fn count(&self) -> Result<i64> {
            Ok(0)
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
            let guard = self.0.lock().expect("lock");
            if guard.fail_documents {
                return Err(Error::Storage("documents failed".to_owned()));
            }
            Ok(guard
                .document
                .clone()
                .filter(|document| document.id == doc_id))
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
            _patch: Map<String, Value>,
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

    impl<'a> TxFactory for Shared<'a> {
        type Tx = FakeTx;
        async fn begin(&self) -> Result<Self::Tx> {
            if self.0.lock().expect("lock").fail_begin {
                return Err(Error::Storage("begin failed".to_owned()));
            }
            Ok(FakeTx)
        }
        async fn commit(&self, _tx: Self::Tx) -> Result<()> {
            if self.0.lock().expect("lock").fail_commit {
                return Err(Error::Storage("commit failed".to_owned()));
            }
            Ok(())
        }
        async fn rollback(&self, _tx: Self::Tx) -> Result<()> {
            Ok(())
        }
    }

    fn service<'a>(
        fakes: &'a Mutex<Fakes>,
    ) -> CitationService<
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        Shared<'a>,
        FakeTx,
        Shared<'a>,
    > {
        CitationService::new(
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
            Shared(fakes),
        )
    }

    fn quoted_request() -> AttachRequest {
        AttachRequest {
            slug: "deror".to_owned(),
            block_key: uuid(BLOCK_KEY),
            intent: "quotation".to_owned(),
            quote: Some("The prophets pair two words.".to_owned()),
            document_id: Some(uuid(DOC_ID)),
            window: None,
            edition_key: Some("DABAR_2026".to_owned()),
            edition_id: None,
            locator: None,
            prefix: None,
            suffix: None,
            placement: "inline".to_owned(),
            citation_key: None,
        }
    }

    fn refused(result: AttachResult<CitationAttached>) -> AttachRefused {
        match result {
            Err(AttachError::Refused(refused)) => refused,
            other => panic!("expected a refusal, got {other:?}"),
        }
    }

    #[test]
    fn test_request_defaults_to_inline_placement() {
        let request = AttachRequest::new("deror".to_owned(), uuid(BLOCK_KEY), "support".to_owned());
        assert_eq!(request.placement, "inline");
        assert_eq!(request.quote, None);
    }

    #[test]
    fn test_narrow_and_warn_intent_sets() {
        assert!(is_narrow_intent(Intent::Quotation));
        assert!(is_narrow_intent(Intent::Translation));
        assert!(!is_narrow_intent(Intent::Support));
        assert!(is_region_warn_intent(Intent::Support));
        assert!(is_region_warn_intent(Intent::Source));
        assert!(is_region_warn_intent(Intent::Definition));
        assert!(!is_region_warn_intent(Intent::Quotation));
        assert!(!is_region_warn_intent(Intent::Background));
    }

    #[test]
    fn test_malformed_intent_fails_before_any_check() {
        // Mirrors `test_malformed_fields_fail_before_any_check`: the intent
        // parses before any repository is touched.
        let fakes = Mutex::new(Fakes::rig());
        let service = service(&fakes);
        let mut request = quoted_request();
        request.intent = "frobnicate".to_owned();
        let error = block_on(service.attach(request)).expect_err("bad intent");
        assert!(
            matches!(&error, AttachError::Failed(Error::Validation(message)) if message == "Unknown intent 'frobnicate'")
        );
    }

    #[test]
    fn test_unknown_placement_fails() {
        let fakes = Mutex::new(Fakes::rig());
        let service = service(&fakes);
        let mut request = quoted_request();
        request.placement = "margin".to_owned();
        let error = block_on(service.attach(request)).expect_err("bad placement");
        assert!(
            matches!(&error, AttachError::Failed(Error::Validation(message)) if message == "Unknown placement 'margin'")
        );
    }

    #[test]
    fn test_frozen_revision_refuses_attach() {
        // Mirrors `test_frozen_is_immutable`: a sealed draft cannot take cites.
        let mut rig = Fakes::rig();
        rig.revision.as_mut().expect("revision").state = RevisionState::Frozen;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.attach(quoted_request())).expect_err("frozen");
        assert!(
            matches!(&error, AttachError::Failed(Error::FrozenRevision(message)) if message == &format!("Revision {REV_ID} is frozen, not draft: copy it forward to edit."))
        );
    }

    #[test]
    fn test_missing_block_is_not_found() {
        let mut rig = Fakes::rig();
        rig.block = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.attach(quoted_request())).expect_err("missing block");
        assert!(matches!(
            error,
            AttachError::Failed(Error::NotFound {
                kind: "work_block",
                ..
            })
        ));
    }

    #[test]
    fn test_no_canonical_text_refuses_without_writing() {
        let mut rig = Fakes::rig();
        rig.verify = VerifyResult {
            tier: VerifyTier::NoCanonicalText,
            location: None,
            matched_fraction: None,
            divergence: None,
        };
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let refused = refused(block_on(service.attach(quoted_request())));
        assert_eq!(refused.rule_id, "AUTH_SOURCE_UNCHECKABLE");
        assert_eq!(
            refused.message,
            format!(
                "Document {DOC_ID} has no canonical text, so the quote cannot be checked. Nothing was written."
            )
        );
        assert_eq!(
            refused
                .detail
                .as_ref()
                .and_then(|detail| detail.get("tier")),
            Some(&Value::String("no_canonical_text".to_owned()))
        );
        assert!(fakes.lock().expect("lock").inserted_items.is_empty());
    }

    #[test]
    fn test_not_found_tier_refuses_with_divergence() {
        let mut rig = Fakes::rig();
        rig.verify = VerifyResult {
            tier: VerifyTier::NotFound,
            location: None,
            matched_fraction: None,
            divergence: Some(VerifyDivergence {
                matched_characters: 3,
                matched_tail: "The".to_owned(),
                quote_continues: " prophets".to_owned(),
                source_continues: " priests".to_owned(),
            }),
        };
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let refused = refused(block_on(service.attach(quoted_request())));
        assert_eq!(refused.rule_id, "AUTH_QUOTE_UNVERIFIED");
        assert_eq!(
            refused.message,
            "Quote verifies not_found: only exact, normalized, or near can be cited. Nothing was written."
        );
        let detail = refused.detail.expect("divergence detail");
        assert_eq!(
            detail.get("tier"),
            Some(&Value::String("not_found".to_owned()))
        );
        let divergence = detail.get("divergence").expect("divergence dump");
        assert_eq!(divergence.get("matched_characters"), Some(&Value::from(3)));
        assert!(fakes.lock().expect("lock").inserted_items.is_empty());
    }

    #[test]
    fn test_unlocatable_near_refuses() {
        let mut rig = Fakes::rig();
        rig.verify = VerifyResult {
            tier: VerifyTier::Near,
            location: None,
            matched_fraction: Some(0.5),
            divergence: None,
        };
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let refused = refused(block_on(service.attach(quoted_request())));
        assert_eq!(refused.rule_id, "AUTH_QUOTE_UNVERIFIED");
        assert_eq!(
            refused.message,
            "Quote verifies near but the matching part will not locate: only a located span can be cited. Nothing was written."
        );
    }

    #[test]
    fn test_quotation_of_a_whole_passage_refuses() {
        // Mirrors `test_narrowing`'s strict leg: region + quotation refuses.
        let fakes = Mutex::new(Fakes::rig().with_region());
        let service = service(&fakes);
        let refused = refused(block_on(service.attach(quoted_request())));
        assert_eq!(refused.rule_id, "AUTH_SPAN_NOT_NARROWED");
        assert_eq!(
            refused.message,
            "A quotation or translation must cite a narrowed span, not a whole passage. Nothing was written."
        );
    }

    #[test]
    fn test_oversized_quotation_refuses_by_size() {
        let mut rig = Fakes::rig();
        rig.verify = VerifyResult {
            tier: VerifyTier::Exact,
            location: Some(VerifyLocation {
                document_id: uuid(DOC_ID),
                char_start: 0,
                char_end: MAX_QUOTE_CHARS + 1,
            }),
            matched_fraction: None,
            divergence: None,
        };
        rig.covering = Vec::new();
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let refused = refused(block_on(service.attach(quoted_request())));
        assert_eq!(refused.rule_id, "AUTH_SPAN_NOT_NARROWED");
    }

    #[test]
    fn test_support_of_a_region_warns() {
        // Mirrors `test_narrowing`'s warned leg.
        let fakes = Mutex::new(Fakes::rig().with_region());
        let service = service(&fakes);
        let mut request = quoted_request();
        request.intent = "support".to_owned();
        let attached = block_on(service.attach(request)).expect("support warns");
        assert_eq!(attached.warnings, vec!["AUTH_SPAN_REGION".to_owned()]);
    }

    #[test]
    fn test_background_of_a_region_stays_quiet() {
        // Mirrors `test_narrowing`'s loose leg: background neither refuses
        // nor warns on a whole passage.
        let fakes = Mutex::new(Fakes::rig().with_region());
        let service = service(&fakes);
        let mut request = quoted_request();
        request.intent = "background".to_owned();
        let attached = block_on(service.attach(request)).expect("background passes");
        assert!(attached.warnings.is_empty());
    }

    #[test]
    fn test_spanless_cite_without_identity_refuses() {
        // Mirrors `test_edition_refused_without_any_source`'s spanless leg.
        let fakes = Mutex::new(Fakes::rig());
        let service = service(&fakes);
        let request = AttachRequest::new("deror".to_owned(), uuid(BLOCK_KEY), "support".to_owned());
        let refused = refused(block_on(service.attach(request)));
        assert_eq!(refused.rule_id, "AUTH_CITATION_EDITION_MISSING");
        assert_eq!(
            refused.message,
            "No edition was given and the cited document names none: pass edition_key or edition_id, or key the document first. Nothing was written."
        );
    }

    #[test]
    fn test_quote_against_keyless_document_refuses_identity() {
        // Mirrors `test_edition_refused_without_any_source`'s quoted leg:
        // nothing to inherit from a document nobody keyed.
        let mut rig = Fakes::rig();
        rig.document.as_mut().expect("document").metadata = Map::new();
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut request = quoted_request();
        request.edition_key = None;
        let refused = refused(block_on(service.attach(request)));
        assert_eq!(refused.rule_id, "AUTH_CITATION_EDITION_MISSING");
    }

    #[test]
    fn test_edition_inherited_from_document() {
        // Mirrors `test_edition_inherited_from_document`.
        let fakes = Mutex::new(Fakes::rig());
        let service = service(&fakes);
        let mut request = quoted_request();
        request.edition_key = None;
        let attached = block_on(service.attach(request)).expect("inherit");
        assert_eq!(attached.item.edition_key.as_deref(), Some("DABAR_2026"));
        assert_eq!(attached.item.edition_id, Some(uuid(EDITION_ID)));
        let guard = fakes.lock().expect("lock");
        assert_eq!(guard.inserted_edition_ids, vec![Some(uuid(EDITION_ID))]);
    }

    #[test]
    fn test_explicit_identity_wins_without_lookup() {
        // Mirrors `test_explicit_identity_wins_and_mismatch_reported`: the
        // given key rides the row verbatim.
        let fakes = Mutex::new(Fakes::rig());
        let service = service(&fakes);
        let mut request = quoted_request();
        request.edition_key = Some("ESV".to_owned());
        let attached = block_on(service.attach(request)).expect("explicit");
        assert_eq!(attached.item.edition_key.as_deref(), Some("ESV"));
        assert_eq!(attached.item.edition_id, None);
    }

    #[test]
    fn test_exact_quote_attaches_with_marker() {
        let fakes = Mutex::new(Fakes::rig());
        let service = service(&fakes);
        let key = Uuid::new_v4();
        let mut request = quoted_request();
        request.citation_key = Some(key);
        let attached = block_on(service.attach(request)).expect("attach");
        assert_eq!(attached.citation_key, key);
        assert_eq!(attached.marker, format!("{{{{cite:{key}}}}}"));
        assert_eq!(attached.item.char_start, Some(34));
        assert_eq!(attached.item.char_end, Some(62));
        assert_eq!(attached.item.verify_status.as_deref(), Some("exact"));
        assert_eq!(attached.item.edition_key.as_deref(), Some("DABAR_2026"));
        assert_eq!(attached.item.edition_id, Some(uuid(EDITION_ID)));
        assert!(attached.item.source_span_id.is_some());
        assert!(attached.warnings.is_empty());
    }
    #[test]
    fn test_refusal_and_failure_displays_report_the_message() {
        // `Display` is the operator-facing surface: refusals and failures
        // both render the underlying message, mirroring the Python text.
        let refused = AttachRefused {
            rule_id: "AUTH_QUOTE_UNVERIFIED".to_owned(),
            message: "Quote verifies not_found.".to_owned(),
            detail: None,
        };
        assert_eq!(refused.to_string(), "Quote verifies not_found.");
        assert_eq!(
            AttachError::Refused(refused).to_string(),
            "Quote verifies not_found."
        );
        assert_eq!(
            AttachError::Failed(Error::Validation("bad".to_owned())).to_string(),
            "data validation failed: bad"
        );
    }

    #[test]
    fn test_error_converts_through_try() {
        // `From<Error>` rides the `?` operator on infrastructure failures.
        fn fallible(fail: bool) -> AttachResult<()> {
            if fail {
                Err(Error::Storage("boom".to_owned()))?;
            }
            Ok(())
        }
        let error = fallible(true).expect_err("boom");
        assert!(matches!(error, AttachError::Failed(Error::Storage(message)) if message == "boom"));
        assert!(fallible(false).is_ok());
    }

    #[test]
    fn test_published_and_superseded_revisions_refuse_attach() {
        // The draft check names whatever sealed state it met; frozen is
        // pinned by `test_frozen_revision_refuses_attach`.
        for (state, name) in [
            (RevisionState::Published, "published"),
            (RevisionState::Superseded, "superseded"),
        ] {
            let mut rig = Fakes::rig();
            rig.revision.as_mut().expect("revision").state = state;
            let fakes = Mutex::new(rig);
            let service = service(&fakes);
            let error = block_on(service.attach(quoted_request())).expect_err("sealed");
            assert!(
                matches!(&error, AttachError::Failed(Error::FrozenRevision(message)) if message == &format!("Revision {REV_ID} is {name}, not draft: copy it forward to edit."))
            );
        }
    }

    #[test]
    fn test_missing_work_is_not_found() {
        let mut rig = Fakes::rig();
        rig.work = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.attach(quoted_request())).expect_err("missing work");
        assert!(matches!(
            error,
            AttachError::Failed(Error::NotFound { kind: "work", .. })
        ));
    }

    #[test]
    fn test_work_without_a_current_revision_is_not_found() {
        let mut rig = Fakes::rig();
        rig.work.as_mut().expect("work").current_revision_id = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.attach(quoted_request())).expect_err("no current");
        assert!(
            matches!(error, AttachError::Failed(Error::NotFound { kind: "work_revision", id }) if id == "current of deror")
        );
    }

    #[test]
    fn test_missing_revision_is_not_found() {
        let mut rig = Fakes::rig();
        rig.revision = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.attach(quoted_request())).expect_err("missing revision");
        assert!(matches!(
            error,
            AttachError::Failed(Error::NotFound {
                kind: "work_revision",
                ..
            })
        ));
    }

    #[test]
    fn test_every_intent_and_placement_attaches() {
        // The full intent vocabulary parses: each request grounds the same
        // quote, and the recorded row echoes the parsed pair back.
        let intents = [
            ("quotation", Intent::Quotation),
            ("translation", Intent::Translation),
            ("support", Intent::Support),
            ("contrast", Intent::Contrast),
            ("background", Intent::Background),
            ("definition", Intent::Definition),
            ("source", Intent::Source),
            ("see_also", Intent::SeeAlso),
        ];
        for (raw, intent) in intents {
            let fakes = Mutex::new(Fakes::rig());
            let service = service(&fakes);
            let mut request = quoted_request();
            request.intent = raw.to_owned();
            request.placement = "block_end".to_owned();
            let attached = block_on(service.attach(request)).expect("attach");
            assert!(attached.marker.starts_with("{{cite:"));
            assert!(attached.warnings.is_empty());
            assert_eq!(
                fakes.lock().expect("lock").recorded,
                vec![(intent, Placement::BlockEnd)]
            );
        }
    }

    #[test]
    fn test_explicit_edition_id_skips_the_key_lookup() {
        // A caller-given edition id wins outright: no key lookup, no
        // inheritance, and the id rides the row verbatim.
        let fakes = Mutex::new(Fakes::rig());
        let service = service(&fakes);
        let mut request = quoted_request();
        request.edition_key = None;
        request.edition_id = Some(uuid(EDITION_ID));
        let attached = block_on(service.attach(request)).expect("explicit id");
        assert_eq!(attached.item.edition_id, Some(uuid(EDITION_ID)));
        assert_eq!(attached.item.edition_key, None);
    }

    #[test]
    fn test_spanless_cite_with_identity_writes_no_span() {
        // A spanless cite names no address: the row stores the edition with
        // a null span, covering the spanless write leg.
        let fakes = Mutex::new(Fakes::rig());
        let service = service(&fakes);
        let mut identified =
            AttachRequest::new("deror".to_owned(), uuid(BLOCK_KEY), "support".to_owned());
        identified.edition_key = Some("DABAR_2026".to_owned());
        let attached = block_on(service.attach(identified)).expect("spanless attach");
        assert!(attached.item.source_span_id.is_none());
        assert_eq!(attached.item.char_start, None);
        assert_eq!(attached.item.verify_status, None);
        assert_eq!(attached.item.edition_key.as_deref(), Some("DABAR_2026"));
    }

    #[test]
    fn test_quote_against_an_unkeyed_document_row_refuses_identity() {
        // The span's document row is gone entirely (not merely keyless), so
        // there is nothing to inherit and the cite refuses.
        let mut rig = Fakes::rig();
        rig.document = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let mut request = quoted_request();
        request.edition_key = None;
        let error = refused(block_on(service.attach(request)));
        assert_eq!(error.rule_id, "AUTH_CITATION_EDITION_MISSING");
    }

    #[test]
    fn test_span_resolve_failure_writes_nothing() {
        let mut rig = Fakes::rig();
        rig.fail_resolve = true;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.attach(quoted_request())).expect_err("resolve fails");
        assert!(
            matches!(error, AttachError::Failed(Error::Storage(message)) if message == "resolve failed")
        );
        assert!(fakes.lock().expect("lock").inserted_items.is_empty());
    }

    #[test]
    fn test_occurrence_failure_writes_nothing() {
        let mut rig = Fakes::rig();
        rig.fail_occurrence = true;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.attach(quoted_request())).expect_err("occurrence fails");
        assert!(
            matches!(error, AttachError::Failed(Error::Storage(message)) if message == "occurrence failed")
        );
        assert!(fakes.lock().expect("lock").inserted_items.is_empty());
    }

    #[test]
    fn test_item_failure_writes_nothing() {
        let mut rig = Fakes::rig();
        rig.fail_item = true;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.attach(quoted_request())).expect_err("item fails");
        assert!(
            matches!(error, AttachError::Failed(Error::Storage(message)) if message == "item failed")
        );
        assert!(fakes.lock().expect("lock").inserted_items.is_empty());
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
    #[should_panic(expected = "expected a refusal")]
    fn test_refused_helper_rejects_a_success() {
        // The `refused` helper only unwraps rule refusals: a success panics
        // rather than fabricating one.
        let fakes = Mutex::new(Fakes::rig());
        let service = service(&fakes);
        let attached = block_on(service.attach(quoted_request())).expect("attach");
        let _ = refused(Ok(attached));
    }
    #[test]
    fn test_fake_ports_surface() {
        // Every fake repo method answers once: unused writers refuse with
        // the sentinel, readers echo the rigged state.
        use marginalia_types::citations::{CitationItemDraft, OccurrenceDraft};
        use marginalia_types::documents::{DocumentDraft, DocumentFilter};
        use marginalia_types::passages::PassageDraft;
        use marginalia_types::ports::FilterExtension;
        use marginalia_types::works::{WorkBlockDraft, WorkDraft, WorkRevisionDraft};

        struct NoFilter;
        impl FilterExtension for NoFilter {
            type Clause = ();
            fn filter_id(&self) -> &str {
                "test_none"
            }
            fn input_schema(&self) -> Map<String, Value> {
                Map::new()
            }
            fn description(&self) -> &str {
                "matches nothing"
            }
            fn build_clause(&self, _value: &Value) -> Result<()> {
                Ok(())
            }
        }

        let fakes = Mutex::new(Fakes::rig());
        let shared = Shared(&fakes);
        let mut unit = ();
        let mut tx = FakeTx;
        assert!(block_on(SourceSpanRepo::get(&shared, Uuid::new_v4()))
            .expect("get")
            .is_none());
        assert!(block_on(shared.for_document(Uuid::new_v4()))
            .expect("for_document")
            .is_empty());
        assert!(block_on(shared.stale(10)).expect("stale").is_empty());
        assert!(block_on(EditionRepo::get(&shared, uuid(EDITION_ID)))
            .expect("get")
            .is_none());
        {
            let error = block_on(shared.upsert_key(&mut unit, "ESV", None, false))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(shared.list_keys()).expect("list_keys").is_empty());
        assert!(block_on(shared.for_block(uuid(BLOCK_ID)))
            .expect("for_block")
            .is_empty());
        assert!(block_on(shared.for_revision(uuid(REV_ID)))
            .expect("for_revision")
            .is_empty());
        assert!(
            block_on(CitationRepo::by_key(&shared, uuid(REV_ID), Uuid::new_v4()))
                .expect("by_key")
                .is_none()
        );
        assert!(block_on(shared.citing_span(Uuid::new_v4()))
            .expect("citing_span")
            .is_empty());
        assert!(block_on(shared.citing_key("ESV"))
            .expect("citing_key")
            .is_empty());
        {
            let error = block_on(WorkRepo::insert(
                &shared,
                &mut unit,
                WorkDraft {
                    slug: "other".to_owned(),
                    title: "Other".to_owned(),
                    work_type: "essay".to_owned(),
                    language: None,
                    abstract_text: None,
                    metadata: Map::new(),
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(WorkRepo::get(&shared, uuid(WORK_ID)))
            .expect("get")
            .is_none());
        assert_eq!(block_on(shared.list()).expect("list").len(), 1);
        block_on(shared.set_current_revision(&mut unit, uuid(WORK_ID), uuid(REV_ID)))
            .expect("set current");
        {
            let error = block_on(shared.update(&mut unit, uuid(WORK_ID), Utc::now(), Map::new()))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.archive(&mut unit, uuid(WORK_ID)))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(WorkRevisionRepo::insert(
                &shared,
                &mut unit,
                WorkRevisionDraft {
                    work_id: uuid(WORK_ID),
                    revision_number: 2,
                    parent_revision_id: None,
                    message: None,
                    created_by: "user".to_owned(),
                    metadata: Map::new(),
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(shared.latest(uuid(WORK_ID)))
            .expect("latest")
            .is_none());
        {
            let error = block_on(shared.copy_forward(&mut unit, uuid(REV_ID)))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.set_message(&mut unit, uuid(REV_ID), "note"))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.freeze(&mut unit, uuid(REV_ID), b"hash"))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.publish(&mut unit, uuid(REV_ID)))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.supersede(&mut unit, uuid(REV_ID)))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.upsert(
                &mut unit,
                uuid(REV_ID),
                WorkBlockDraft {
                    revision_id: uuid(REV_ID),
                    block_key: Uuid::new_v4(),
                    parent_id: None,
                    position: 0,
                    block_type: "paragraph".to_owned(),
                    title: None,
                    body_markdown: "words".to_owned(),
                    attributes: Map::new(),
                },
                None,
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(shared.tree(uuid(REV_ID)))
            .expect("tree")
            .is_empty());
        assert!(block_on(WorkBlockRepo::get(&shared, uuid(BLOCK_ID)))
            .expect("get")
            .is_none());
        assert!(block_on(WorkBlockRepo::by_key_in_tx(
            &shared,
            &mut unit,
            uuid(REV_ID),
            uuid(BLOCK_KEY)
        ))
        .expect("by_key_in_tx")
        .is_none());
        block_on(WorkBlockRepo::delete(&shared, &mut unit, uuid(BLOCK_ID))).expect("delete");
        {
            let error = block_on(shared.get_context(Uuid::new_v4(), 1, 1))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(
            block_on(shared.insert_many(&mut unit, uuid(DOC_ID), Vec::<PassageDraft>::new()))
                .expect("insert_many")
                .is_empty()
        );
        assert!(block_on(PassageRepo::get(&shared, Uuid::new_v4()))
            .expect("get")
            .is_none());
        assert!(block_on(shared.get_by_document(uuid(DOC_ID)))
            .expect("by document")
            .is_empty());
        assert!(
            block_on(shared.vector_search(&[0.1], "model", "1", None, 5))
                .expect("vector")
                .is_empty()
        );
        assert!(block_on(shared.keyword_search("prophets", None, None, 5))
            .expect("keyword")
            .is_empty());
        block_on(shared.store_embeddings(&mut unit, &[], &[], "model", "1", 3))
            .expect("store embeddings");
        block_on(shared.index_fts(&mut unit, &[], &[], "en")).expect("index");
        assert!(block_on(shared.get_embedding(Uuid::new_v4(), "model", "1"))
            .expect("embedding")
            .is_none());
        let none: Option<&HashMap<String, NoFilter>> = None;
        assert!(block_on(PassageRepo::filter_candidate_ids::<NoFilter>(
            &shared,
            &Map::new(),
            none
        ))
        .expect("filter")
        .is_empty());
        let filter = NoFilter;
        assert_eq!(filter.filter_id(), "test_none");
        assert_eq!(filter.description(), "matches nothing");
        assert!(filter.input_schema().is_empty());
        filter.build_clause(&Value::Null).expect("clause");
        assert!(block_on(shared.covering_span(uuid(DOC_ID), 0, 4))
            .expect("covering")
            .is_empty());
        assert_eq!(block_on(PassageRepo::count(&shared)).expect("count"), 0);
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
                    metadata: Map::new(),
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
            let error = block_on(shared.update_metadata(uuid(DOC_ID), Map::new()))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(shared.iter_by_filter(&DocumentFilter::default()))
            .expect("iter")
            .is_empty());
        assert_eq!(
            block_on(DocumentRepo::count(&shared, None)).expect("count"),
            0
        );
        block_on(DocumentRepo::delete(&shared, uuid(DOC_ID))).expect("delete");
        let key = Uuid::new_v4();
        let stored = block_on(shared.insert_occurrence(
            &mut tx,
            OccurrenceDraft {
                block_id: uuid(BLOCK_ID),
                citation_key: key,
                placement: Placement::Inline,
                intent: Intent::Quotation,
                note: None,
            },
        ))
        .expect("insert occurrence");
        assert_eq!(stored.citation_key, key);
        assert_eq!(stored.block_id, uuid(BLOCK_ID));
        let item = block_on(shared.insert_item(
            &mut tx,
            CitationItemDraft {
                occurrence_id: stored.id,
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
        .expect("insert item");
        assert_eq!(item.edition_key.as_deref(), Some("DABAR_2026"));
        assert_eq!(fakes.lock().expect("lock").inserted_items.len(), 1);
        block_on(shared.rollback(FakeTx)).expect("rollback");
    }

    #[test]
    fn test_infrastructure_failures_surface_without_writing() {
        // Every repository read and transaction boundary reports storage
        // errors as `Failed`: the attach writes nothing on the way out.
        // Each case names the rig flag, the request shape, and the message.
        fn rigged(flag: fn(&mut Fakes)) -> Mutex<Fakes> {
            let mut rig = Fakes::rig();
            flag(&mut rig);
            Mutex::new(rig)
        }
        fn quoted() -> AttachRequest {
            quoted_request()
        }
        fn unkeyed() -> AttachRequest {
            let mut request = quoted_request();
            request.edition_key = None;
            request
        }
        fn supported() -> AttachRequest {
            let mut request = quoted_request();
            request.intent = "support".to_owned();
            request
        }
        let cases = [
            (
                (|rig: &mut Fakes| rig.fail_works = true) as fn(&mut Fakes),
                quoted as fn() -> AttachRequest,
                "works failed",
            ),
            (
                (|rig: &mut Fakes| rig.fail_revisions = true) as fn(&mut Fakes),
                quoted as fn() -> AttachRequest,
                "revisions failed",
            ),
            (
                (|rig: &mut Fakes| rig.fail_blocks = true) as fn(&mut Fakes),
                quoted as fn() -> AttachRequest,
                "blocks failed",
            ),
            (
                (|rig: &mut Fakes| rig.fail_editions = true) as fn(&mut Fakes),
                quoted as fn() -> AttachRequest,
                "editions failed",
            ),
            (
                (|rig: &mut Fakes| rig.fail_verify = true) as fn(&mut Fakes),
                quoted as fn() -> AttachRequest,
                "verify failed",
            ),
            (
                (|rig: &mut Fakes| rig.fail_covering = true) as fn(&mut Fakes),
                quoted as fn() -> AttachRequest,
                "covering failed",
            ),
            (
                (|rig: &mut Fakes| rig.fail_covering = true) as fn(&mut Fakes),
                supported as fn() -> AttachRequest,
                "covering failed",
            ),
            (
                (|rig: &mut Fakes| rig.fail_documents = true) as fn(&mut Fakes),
                unkeyed as fn() -> AttachRequest,
                "documents failed",
            ),
            (
                (|rig: &mut Fakes| rig.fail_editions = true) as fn(&mut Fakes),
                unkeyed as fn() -> AttachRequest,
                "editions failed",
            ),
            (
                (|rig: &mut Fakes| rig.fail_begin = true) as fn(&mut Fakes),
                quoted as fn() -> AttachRequest,
                "begin failed",
            ),
            (
                (|rig: &mut Fakes| rig.fail_commit = true) as fn(&mut Fakes),
                quoted as fn() -> AttachRequest,
                "commit failed",
            ),
        ];
        for (flag, request, message) in cases {
            let fakes = rigged(flag);
            let service = service(&fakes);
            let error = block_on(service.attach(request())).expect_err("infra fails");
            assert!(
                matches!(error, AttachError::Failed(Error::Storage(failure)) if failure == message),
                "wrong failure for {message}"
            );
            // Commit runs last, so its failure is the one case with a
            // stored row behind it; every earlier failure writes nothing.
            assert_eq!(
                fakes.lock().expect("lock").inserted_items.is_empty(),
                message != "commit failed"
            );
        }
    }
}
