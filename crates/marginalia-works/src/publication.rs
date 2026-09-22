//! Freeze and publish revisions — the human act that commits.
//!
//! Python source: `services/works/publication.py`. `freeze` validates at the
//! freeze gate, refuses on blockers, inserts the waivers it was given, and
//! stores the content hash. `publish` validates at the publish gate, where
//! edition identity graduates from warning to error. Both run in one
//! transaction; a refusal writes nothing, waivers included.

use std::collections::HashSet;
use std::fmt;

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use marginalia_types::ports::{
    CitationRepo, SourceSpanRepo, WaiverRepo, WorkBlockRepo, WorkLinkRepo, WorkRepo,
    WorkRevisionRepo,
};
use marginalia_types::works::WaiverDraft;
use marginalia_types::works_ports::TxFactory;
use marginalia_types::{Error, Result};

use crate::assembly::{assemble_revision, hash_assembled};
use crate::validate::ValidateGate;

/// The revision failed its gate; waivers and the freeze were not written.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FreezeBlocked {
    pub blockers: Vec<String>,
}

impl fmt::Display for FreezeBlocked {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "Revision is blocked by: {}. Pass waivers for the answered ones, or fix the rest.",
            self.blockers.join(", ")
        )
    }
}

impl std::error::Error for FreezeBlocked {}

/// Publish/freeze failure: a gate refusal, or an infrastructure error.
#[derive(Debug)]
pub enum PublicationError {
    Blocked(FreezeBlocked),
    Failed(Error),
}

impl fmt::Display for PublicationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Blocked(blocked) => write!(formatter, "{blocked}"),
            Self::Failed(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for PublicationError {}

impl From<Error> for PublicationError {
    fn from(error: Error) -> Self {
        Self::Failed(error)
    }
}

pub type PublicationResult<T> = std::result::Result<T, PublicationError>;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WaiverGiven {
    pub rule_id: String,
    #[serde(default)]
    pub subject: Option<String>,
    pub reason: String,
    #[serde(default = "default_actor")]
    pub actor: String,
}

fn default_actor() -> String {
    "user".to_owned()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RevisionSealed {
    pub revision_id: Uuid,
    pub revision_number: i64,
    #[serde(default)]
    pub content_hash: Option<String>,
    pub state: String,
    #[serde(default)]
    pub waivers: Vec<String>,
}

/// Lowercase hex, exactly `bytes.hex()` on the Python side.
fn hex_lower(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

fn revision_state_value(state: marginalia_types::works::RevisionState) -> &'static str {
    match state {
        marginalia_types::works::RevisionState::Draft => "draft",
        marginalia_types::works::RevisionState::Frozen => "frozen",
        marginalia_types::works::RevisionState::Published => "published",
        marginalia_types::works::RevisionState::Superseded => "superseded",
    }
}

/// Gate, waive, hash, and seal.
pub struct WorkPublicationService<Val, W, R, B, C, L, S, Wv, Tx, F> {
    validation: Val,
    works: W,
    revisions: R,
    blocks: B,
    citations: C,
    links: L,
    spans: S,
    waivers: Wv,
    tx_factory: F,
    tx_marker: std::marker::PhantomData<Tx>,
}

impl<Val, W, R, B, C, L, S, Wv, Tx, F> WorkPublicationService<Val, W, R, B, C, L, S, Wv, Tx, F>
where
    Val: ValidationPort,
    W: WorkRepo,
    R: WorkRevisionRepo<Tx = Tx>,
    B: WorkBlockRepo,
    C: CitationRepo,
    L: WorkLinkRepo,
    S: SourceSpanRepo,
    Wv: WaiverRepo<Tx = Tx>,
    F: TxFactory<Tx = Tx>,
{
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        validation: Val,
        works: W,
        revisions: R,
        blocks: B,
        citations: C,
        links: L,
        spans: S,
        waivers: Wv,
        tx_factory: F,
    ) -> Self {
        Self {
            validation,
            works,
            revisions,
            blocks,
            citations,
            links,
            spans,
            waivers,
            tx_factory,
            tx_marker: std::marker::PhantomData,
        }
    }

    /// Validate at freeze, record waivers, hash, and seal the draft.
    pub async fn freeze(
        &self,
        slug: &str,
        message: Option<&str>,
        waivers: &[WaiverGiven],
    ) -> PublicationResult<RevisionSealed> {
        let work = self
            .works
            .get_by_slug(slug)
            .await
            .map_err(PublicationError::Failed)?
            .ok_or_else(|| {
                PublicationError::Failed(Error::NotFound {
                    kind: "work",
                    id: slug.to_owned(),
                })
            })?;
        let prospective: HashSet<(String, Option<String>)> = waivers
            .iter()
            .map(|waiver| (waiver.rule_id.clone(), waiver.subject.clone()))
            .collect();
        let report = self
            .validation
            .validate_for_gate(slug, ValidateGate::Freeze, &prospective)
            .await
            .map_err(PublicationError::Failed)?;
        if !report.passed {
            return Err(PublicationError::Blocked(FreezeBlocked {
                blockers: report.blockers,
            }));
        }
        // proof: the freeze gate above only passes on a resolvable draft, so
        // both pointers below are whole (Python marks the same two lines
        // `pragma: no cover`).
        let current_id = work.current_revision_id.ok_or_else(|| {
            PublicationError::Failed(Error::NotFound {
                kind: "work_revision",
                id: format!("current of {slug}"),
            })
        })?;
        let revision = self
            .revisions
            .get(current_id)
            .await
            .map_err(PublicationError::Failed)?
            .ok_or_else(|| {
                PublicationError::Failed(Error::NotFound {
                    kind: "work_revision",
                    id: current_id.to_string(),
                })
            })?;
        let view = assemble_revision(
            &work,
            &revision,
            &self.blocks,
            &self.citations,
            &self.links,
            &self.spans,
        )
        .await
        .map_err(PublicationError::Failed)?;
        let content_hash = hash_assembled(&view);

        let mut tx = self
            .tx_factory
            .begin()
            .await
            .map_err(PublicationError::Failed)?;
        for waiver in waivers {
            if let Err(error) = self
                .waivers
                .insert(
                    &mut tx,
                    WaiverDraft {
                        revision_id: revision.id,
                        rule_id: waiver.rule_id.clone(),
                        subject: waiver.subject.clone(),
                        actor: waiver.actor.clone(),
                        reason: waiver.reason.clone(),
                    },
                )
                .await
            {
                let _ = self.tx_factory.rollback(tx).await;
                return Err(PublicationError::Failed(error));
            }
        }
        let sealed = match self
            .revisions
            .freeze(&mut tx, revision.id, &content_hash)
            .await
        {
            Ok(sealed) => sealed,
            Err(error) => {
                let _ = self.tx_factory.rollback(tx).await;
                return Err(PublicationError::Failed(error));
            }
        };
        let sealed = match message {
            Some(message) => match self
                .revisions
                .set_message(&mut tx, revision.id, message)
                .await
            {
                Ok(sealed) => sealed,
                Err(error) => {
                    let _ = self.tx_factory.rollback(tx).await;
                    return Err(PublicationError::Failed(error));
                }
            },
            None => sealed,
        };
        self.tx_factory
            .commit(tx)
            .await
            .map_err(PublicationError::Failed)?;
        Ok(RevisionSealed {
            revision_id: sealed.id,
            revision_number: sealed.revision_number,
            content_hash: Some(hex_lower(&content_hash)),
            state: revision_state_value(sealed.state).to_owned(),
            waivers: waivers
                .iter()
                .map(|waiver| waiver.rule_id.clone())
                .collect(),
        })
    }

    /// Validate at publish, then seal the frozen revision as published.
    pub async fn publish(&self, slug: &str) -> PublicationResult<RevisionSealed> {
        let work = self
            .works
            .get_by_slug(slug)
            .await
            .map_err(PublicationError::Failed)?
            .ok_or_else(|| {
                PublicationError::Failed(Error::NotFound {
                    kind: "work",
                    id: slug.to_owned(),
                })
            })?;
        let report = self
            .validation
            .validate_for_gate(slug, ValidateGate::Publish, &HashSet::new())
            .await
            .map_err(PublicationError::Failed)?;
        if !report.passed {
            return Err(PublicationError::Blocked(FreezeBlocked {
                blockers: report.blockers,
            }));
        }
        // proof: the publish gate above only passes on a resolvable revision
        // (Python marks this `pragma: no cover`).
        let current_id = work.current_revision_id.ok_or_else(|| {
            PublicationError::Failed(Error::NotFound {
                kind: "work_revision",
                id: format!("current of {slug}"),
            })
        })?;
        let mut tx = self
            .tx_factory
            .begin()
            .await
            .map_err(PublicationError::Failed)?;
        let sealed = match self.revisions.publish(&mut tx, current_id).await {
            Ok(sealed) => sealed,
            Err(error) => {
                let _ = self.tx_factory.rollback(tx).await;
                return Err(PublicationError::Failed(error));
            }
        };
        self.tx_factory
            .commit(tx)
            .await
            .map_err(PublicationError::Failed)?;
        Ok(RevisionSealed {
            revision_id: sealed.id,
            revision_number: sealed.revision_number,
            content_hash: sealed.content_hash.as_ref().map(|hash| hex_lower(hash)),
            state: revision_state_value(sealed.state).to_owned(),
            waivers: Vec::new(),
        })
    }
}

/// The validation seam `freeze`/`publish` call: `validate` at a gate with
/// prospective waivers, returning pass/fail plus blockers. Implemented by
/// [`WorkValidationService`](crate::validate::WorkValidationService); fakes stand in for tests.
// The port-trait seam is `async fn` like every `marginalia_types` port; the
// signature is contract, so the lint is allowed here rather than desugared.
#[allow(async_fn_in_trait)]
pub trait ValidationPort: Send + Sync {
    async fn validate_for_gate(
        &self,
        slug: &str,
        gate: ValidateGate,
        prospective: &std::collections::HashSet<(String, Option<String>)>,
    ) -> Result<ValidationGateReport>;
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ValidationGateReport {
    pub passed: bool,
    #[serde(default)]
    pub blockers: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::future::Future;
    use std::pin::Pin;
    use std::sync::Mutex;
    use std::task::{Context, Poll, RawWaker, RawWakerVTable, Waker};

    use chrono::Utc;
    use marginalia_types::citations::BlockCitations;
    use marginalia_types::spans::SourceSpan;
    use marginalia_types::works::{
        BlockLinks, RevisionState, Waiver, Work, WorkBlock, WorkRevision, WorkStatus,
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

    fn uuid(raw: &str) -> Uuid {
        Uuid::parse_str(raw).expect("fixture uuid")
    }

    #[derive(Debug)]
    struct FakeTx;

    struct Fakes {
        work: Option<Work>,
        revision: Option<WorkRevision>,
        blocks: Vec<WorkBlock>,
        report: ValidationGateReport,
        waivers: Vec<marginalia_types::works::WaiverDraft>,
        fail_works: bool,
        fail_validation: bool,
        fail_revisions: bool,
        fail_tree: bool,
        fail_begin: bool,
        fail_commit: bool,
        attached: Vec<BlockCitations>,
        link_sources: Vec<marginalia_types::works::BlockSourceLink>,
        message: Option<String>,
        frozen_hash: Option<Vec<u8>>,
        published: bool,
        fail_waiver: bool,
        fail_freeze: bool,
        fail_message: bool,
        fail_publish: bool,
    }

    impl Fakes {
        fn rig() -> Self {
            let now = Utc::now();
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
                    metadata: serde_json::Map::new(),
                    created_at: now,
                    updated_at: now,
                    archived_at: None,
                }),
                blocks: Vec::new(),
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
                    metadata: serde_json::Map::new(),
                }),
                report: ValidationGateReport {
                    passed: true,
                    blockers: Vec::new(),
                },
                waivers: Vec::new(),
                fail_works: false,
                fail_validation: false,
                fail_revisions: false,
                fail_tree: false,
                fail_begin: false,
                fail_commit: false,
                attached: Vec::new(),
                link_sources: Vec::new(),
                message: None,
                frozen_hash: None,
                published: false,
                fail_waiver: false,
                fail_freeze: false,
                fail_message: false,
                fail_publish: false,
            }
        }

        fn blocked() -> Self {
            let mut rig = Self::rig();
            rig.report = ValidationGateReport {
                passed: false,
                blockers: vec!["AUTH_QUOTE_UNVERIFIED".to_owned()],
            };
            rig
        }
    }

    struct Shared<'a>(&'a Mutex<Fakes>);

    impl<'a> ValidationPort for Shared<'a> {
        async fn validate_for_gate(
            &self,
            _slug: &str,
            _gate: ValidateGate,
            _prospective: &HashSet<(String, Option<String>)>,
        ) -> Result<ValidationGateReport> {
            let guard = self.0.lock().expect("lock");
            if guard.fail_validation {
                return Err(Error::Storage("validation failed".to_owned()));
            }
            Ok(guard.report.clone())
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
            _fields: serde_json::Map<String, serde_json::Value>,
        ) -> Result<Work> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn archive(&self, _tx: &mut Self::Tx, _work_id: Uuid) -> Result<Work> {
            Err(Error::Storage("unused".to_owned()))
        }
    }

    impl<'a> WorkRevisionRepo for Shared<'a> {
        type Tx = FakeTx;
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
            revision_id: Uuid,
            message: &str,
        ) -> Result<WorkRevision> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_message {
                return Err(Error::Storage("message failed".to_owned()));
            }
            guard.message = Some(message.to_owned());
            let mut revision = guard.revision.clone().expect("revision");
            assert_eq!(revision.id, revision_id);
            revision.message = Some(message.to_owned());
            guard.revision = Some(revision.clone());
            Ok(revision)
        }
        async fn freeze(
            &self,
            _tx: &mut Self::Tx,
            revision_id: Uuid,
            content_hash: &[u8],
        ) -> Result<WorkRevision> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_freeze {
                return Err(Error::Storage("freeze failed".to_owned()));
            }
            guard.frozen_hash = Some(content_hash.to_vec());
            let mut revision = guard.revision.clone().expect("revision");
            assert_eq!(revision.id, revision_id);
            revision.state = RevisionState::Frozen;
            revision.content_hash = Some(content_hash.to_vec());
            guard.revision = Some(revision.clone());
            Ok(revision)
        }
        async fn publish(&self, _tx: &mut Self::Tx, revision_id: Uuid) -> Result<WorkRevision> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_publish {
                return Err(Error::Storage("publish failed".to_owned()));
            }
            guard.published = true;
            let mut revision = guard.revision.clone().expect("revision");
            assert_eq!(revision.id, revision_id);
            revision.state = RevisionState::Published;
            guard.revision = Some(revision.clone());
            Ok(revision)
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
            let guard = self.0.lock().expect("lock");
            if guard.fail_tree {
                return Err(Error::Storage("tree failed".to_owned()));
            }
            Ok(guard.blocks.clone())
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

    impl<'a> CitationRepo for Shared<'a> {
        type Tx = ();
        async fn insert_occurrence(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::citations::OccurrenceDraft,
        ) -> Result<marginalia_types::citations::CitationOccurrence> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn insert_item(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::citations::CitationItemDraft,
        ) -> Result<marginalia_types::citations::CitationItem> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn for_block(&self, _block_id: Uuid) -> Result<Vec<BlockCitations>> {
            Ok(Vec::new())
        }
        async fn for_revision(&self, _revision_id: Uuid) -> Result<Vec<BlockCitations>> {
            Ok(self.0.lock().expect("lock").attached.clone())
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

    impl<'a> WorkLinkRepo for Shared<'a> {
        type Tx = ();
        async fn add_source_link(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::works::BlockSourceLinkDraft,
        ) -> Result<marginalia_types::works::BlockSourceLink> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn add_entity_link(
            &self,
            _tx: &mut Self::Tx,
            _draft: marginalia_types::works::BlockEntityLinkDraft,
        ) -> Result<marginalia_types::works::BlockEntityLink> {
            Err(Error::Storage("unused".to_owned()))
        }
        async fn for_block(&self, _block_id: Uuid) -> Result<BlockLinks> {
            Ok(BlockLinks {
                sources: self.0.lock().expect("lock").link_sources.clone(),
                entities: Vec::new(),
            })
        }
        async fn for_span(
            &self,
            _span_id: Uuid,
        ) -> Result<Vec<marginalia_types::works::BlockSourceLink>> {
            Ok(Vec::new())
        }
        async fn for_entity(
            &self,
            _entity_id: Uuid,
            _relation: &str,
        ) -> Result<Vec<marginalia_types::works::BlockEntityLink>> {
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

    impl<'a> WaiverRepo for Shared<'a> {
        type Tx = FakeTx;
        async fn insert(
            &self,
            _tx: &mut Self::Tx,
            draft: marginalia_types::works::WaiverDraft,
        ) -> Result<Waiver> {
            let mut guard = self.0.lock().expect("lock");
            if guard.fail_waiver {
                return Err(Error::Storage("waiver failed".to_owned()));
            }
            guard.waivers.push(draft.clone());
            Ok(Waiver {
                id: Uuid::new_v4(),
                revision_id: draft.revision_id,
                rule_id: draft.rule_id,
                subject: draft.subject,
                actor: draft.actor,
                reason: draft.reason,
                created_at: Utc::now(),
            })
        }
        async fn for_revision(&self, _revision_id: Uuid) -> Result<Vec<Waiver>> {
            Ok(Vec::new())
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
    ) -> WorkPublicationService<
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
        WorkPublicationService::new(
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

    fn waiver() -> WaiverGiven {
        WaiverGiven {
            rule_id: "AUTH_QUOTE_UNVERIFIED".to_owned(),
            subject: Some("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa".to_owned()),
            reason: "OCR noise reviewed against the scan".to_owned(),
            actor: "user".to_owned(),
        }
    }

    #[test]
    fn test_blockers_display_names_waivers_or_fixes() {
        let blocked = FreezeBlocked {
            blockers: vec!["AUTH_QUOTE_UNVERIFIED".to_owned()],
        };
        assert_eq!(
            blocked.to_string(),
            "Revision is blocked by: AUTH_QUOTE_UNVERIFIED. Pass waivers for the answered ones, or fix the rest."
        );
    }

    #[test]
    fn test_waiver_actor_defaults_to_user() {
        let parsed: WaiverGiven = serde_json::from_value(serde_json::json!({
            "rule_id": "AUTH_QUOTE_UNVERIFIED",
            "reason": "reviewed",
        }))
        .expect("waiver parses");
        assert_eq!(parsed.actor, "user");
        assert_eq!(parsed.subject, None);
    }

    #[test]
    fn test_freeze_is_blocked_before_any_write() {
        // Mirrors `test_freeze_waiver_and_stable_hash`'s first leg: blockers
        // refuse, and the waiver rows stay unwritten.
        let fakes = Mutex::new(Fakes::blocked());
        let service = service(&fakes);
        let error = block_on(service.freeze("deror", None, &[])).expect_err("blocked");
        assert!(
            matches!(&error, PublicationError::Blocked(blocked) if blocked.blockers == ["AUTH_QUOTE_UNVERIFIED".to_owned()])
        );
        let guard = fakes.lock().expect("lock");
        assert!(guard.waivers.is_empty());
        assert!(guard.frozen_hash.is_none());
    }

    #[test]
    fn test_freeze_seals_with_waivers_and_stable_hash() {
        // Mirrors `test_freeze_waiver_and_stable_hash`'s second leg: the
        // waiver row, the stored hash, and the sealed summary agree. A
        // block rides along so the tree walk joins rows like production.
        let mut rig = Fakes::rig();
        let cited_at = Utc::now();
        rig.attached.push(BlockCitations {
            occurrence: marginalia_types::citations::CitationOccurrence {
                id: uuid(REV_ID),
                citation_key: uuid(REV_ID),
                block_id: uuid(REV_ID),
                placement: marginalia_types::works::Placement::Inline,
                intent: marginalia_types::works_files::Intent::Quotation,
                note: None,
                created_at: cited_at,
            },
            items: vec![marginalia_types::citations::CitationItem {
                occurrence_id: uuid(REV_ID),
                position: 0,
                edition_id: None,
                edition_key: Some("DABAR_2026".to_owned()),
                source_span_id: None,
                quoted_text: None,
                verify_status: None,
                verified_at: None,
                locator: serde_json::Map::new(),
                prefix: None,
                suffix: None,
                suppress_author: false,
            }],
        });
        rig.link_sources
            .push(marginalia_types::works::BlockSourceLink {
                block_id: uuid(REV_ID),
                source_span_id: Uuid::new_v4(),
                relation: "discusses".to_owned(),
                confidence: None,
                note: None,
                created_at: cited_at,
            });
        rig.blocks.push(WorkBlock {
            id: uuid(REV_ID),
            revision_id: uuid(REV_ID),
            block_key: uuid(REV_ID),
            parent_id: None,
            position: 0,
            block_type: "paragraph".to_owned(),
            title: None,
            body_markdown: "Deror.".to_owned(),
            attributes: serde_json::Map::new(),
            created_at: Utc::now(),
            updated_at: Utc::now(),
        });
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let sealed =
            block_on(service.freeze("deror", Some("first"), std::slice::from_ref(&waiver())))
                .expect("freeze");
        assert_eq!(sealed.revision_id, uuid(REV_ID));
        assert_eq!(sealed.revision_number, 1);
        assert_eq!(sealed.state, "frozen");
        assert_eq!(sealed.waivers, vec!["AUTH_QUOTE_UNVERIFIED".to_owned()]);
        let guard = fakes.lock().expect("lock");
        assert_eq!(guard.waivers.len(), 1);
        assert_eq!(guard.waivers[0].rule_id, "AUTH_QUOTE_UNVERIFIED");
        assert_eq!(
            guard.waivers[0].subject.as_deref(),
            Some("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        );
        assert_eq!(guard.waivers[0].actor, "user");
        assert_eq!(guard.message.as_deref(), Some("first"));
        let stored = guard.frozen_hash.clone().expect("stored hash");
        assert_eq!(Some(hex_lower(&stored)), sealed.content_hash);
        // The hash covers authored content only: freezing the same empty view
        // twice stores the same bytes.
        let work = guard.work.clone().expect("work");
        let revision = guard.revision.clone().expect("revision");
        drop(guard);
        let view = block_on(crate::assembly::assemble_revision(
            &work,
            &revision,
            &Shared(&fakes),
            &Shared(&fakes),
            &Shared(&fakes),
            &Shared(&fakes),
        ))
        .expect("reassemble");
        assert_eq!(crate::assembly::hash_assembled(&view), stored.as_slice());
    }

    #[test]
    fn test_freeze_without_message_skips_the_message_write() {
        let fakes = Mutex::new(Fakes::rig());
        let service = service(&fakes);
        let sealed = block_on(service.freeze("deror", None, &[])).expect("freeze");
        assert_eq!(sealed.state, "frozen");
        assert!(sealed.waivers.is_empty());
        assert!(fakes.lock().expect("lock").message.is_none());
    }

    #[test]
    fn test_freeze_missing_work_is_not_found() {
        let mut rig = Fakes::rig();
        rig.work = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.freeze("unknown", None, &[])).expect_err("missing");
        assert!(matches!(
            error,
            PublicationError::Failed(Error::NotFound { kind: "work", .. })
        ));
    }

    #[test]
    fn test_publish_seals_the_frozen_revision() {
        // Mirrors `test_publish`: freeze, then publish to `published`.
        let fakes = Mutex::new(Fakes::rig());
        let service = service(&fakes);
        block_on(service.freeze("deror", Some("reviewed"), &[])).expect("freeze");
        let sealed = block_on(service.publish("deror")).expect("publish");
        assert_eq!(sealed.state, "published");
        assert_eq!(sealed.revision_number, 1);
        let guard = fakes.lock().expect("lock");
        assert!(guard.published);
        let stored = guard
            .revision
            .as_ref()
            .expect("revision")
            .content_hash
            .clone()
            .map(|hash| hex_lower(&hash));
        assert_eq!(sealed.content_hash, stored);
    }

    #[test]
    fn test_publish_is_blocked_at_its_gate() {
        let fakes = Mutex::new(Fakes::blocked());
        let service = service(&fakes);
        let error = block_on(service.publish("deror")).expect_err("blocked");
        assert!(
            matches!(&error, PublicationError::Blocked(blocked) if blocked.blockers == ["AUTH_QUOTE_UNVERIFIED".to_owned()])
        );
        assert!(!fakes.lock().expect("lock").published);
    }
    #[test]
    fn test_blocked_and_failure_displays_report_the_cause() {
        // `Display` is the operator-facing surface: a gate refusal names
        // its blockers, a failure renders the underlying error.
        let blocked = FreezeBlocked {
            blockers: vec!["AUTH_QUOTE_UNVERIFIED".to_owned()],
        };
        assert_eq!(
            PublicationError::Blocked(blocked).to_string(),
            "Revision is blocked by: AUTH_QUOTE_UNVERIFIED. Pass waivers for the answered ones, or fix the rest."
        );
        assert_eq!(
            PublicationError::Failed(Error::Validation("bad".to_owned())).to_string(),
            "data validation failed: bad"
        );
    }

    #[test]
    fn test_error_converts_through_try() {
        // `From<Error>` rides the `?` operator on infrastructure failures.
        fn fallible(fail: bool) -> PublicationResult<()> {
            if fail {
                Err(Error::Storage("boom".to_owned()))?;
            }
            Ok(())
        }
        let error = fallible(true).expect_err("boom");
        assert!(fallible(false).is_ok());
        assert!(
            matches!(error, PublicationError::Failed(Error::Storage(message)) if message == "boom")
        );
    }

    #[test]
    fn test_revision_state_values_match_python() {
        // The sealed summary interpolates `RevisionState.value`; frozen and
        // published ride the service tests, draft and superseded pin here.
        assert_eq!(revision_state_value(RevisionState::Draft), "draft");
        assert_eq!(revision_state_value(RevisionState::Frozen), "frozen");
        assert_eq!(revision_state_value(RevisionState::Published), "published");
        assert_eq!(
            revision_state_value(RevisionState::Superseded),
            "superseded"
        );
    }

    #[test]
    fn test_freeze_without_a_current_revision_is_not_found() {
        let mut rig = Fakes::rig();
        rig.work.as_mut().expect("work").current_revision_id = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.freeze("deror", None, &[])).expect_err("no current");
        assert!(
            matches!(error, PublicationError::Failed(Error::NotFound { kind: "work_revision", id }) if id == "current of deror")
        );
    }

    #[test]
    fn test_freeze_with_a_missing_revision_is_not_found() {
        let mut rig = Fakes::rig();
        rig.revision = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.freeze("deror", None, &[])).expect_err("missing revision");
        assert!(matches!(
            error,
            PublicationError::Failed(Error::NotFound {
                kind: "work_revision",
                ..
            })
        ));
    }

    #[test]
    fn test_waiver_write_failure_writes_nothing() {
        let mut rig = Fakes::rig();
        rig.fail_waiver = true;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.freeze("deror", None, std::slice::from_ref(&waiver())))
            .expect_err("waiver fails");
        assert!(
            matches!(error, PublicationError::Failed(Error::Storage(message)) if message == "waiver failed")
        );
        let guard = fakes.lock().expect("lock");
        assert!(guard.waivers.is_empty());
        assert!(guard.frozen_hash.is_none());
    }

    #[test]
    fn test_freeze_write_failure_leaves_the_draft_open() {
        let mut rig = Fakes::rig();
        rig.fail_freeze = true;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.freeze("deror", None, &[])).expect_err("freeze fails");
        assert!(
            matches!(error, PublicationError::Failed(Error::Storage(message)) if message == "freeze failed")
        );
        let guard = fakes.lock().expect("lock");
        assert!(guard.frozen_hash.is_none());
        assert_eq!(
            guard.revision.as_ref().expect("revision").state,
            RevisionState::Draft
        );
    }

    #[test]
    fn test_message_write_failure_reports_without_setting_it() {
        let mut rig = Fakes::rig();
        rig.fail_message = true;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error =
            block_on(service.freeze("deror", Some("first"), &[])).expect_err("message fails");
        assert!(
            matches!(error, PublicationError::Failed(Error::Storage(message)) if message == "message failed")
        );
        assert!(fakes.lock().expect("lock").message.is_none());
    }

    #[test]
    fn test_publish_missing_work_is_not_found() {
        let mut rig = Fakes::rig();
        rig.work = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.publish("unknown")).expect_err("missing");
        assert!(matches!(
            error,
            PublicationError::Failed(Error::NotFound { kind: "work", .. })
        ));
    }

    #[test]
    fn test_publish_without_a_current_revision_is_not_found() {
        let mut rig = Fakes::rig();
        rig.work.as_mut().expect("work").current_revision_id = None;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.publish("deror")).expect_err("no current");
        assert!(
            matches!(error, PublicationError::Failed(Error::NotFound { kind: "work_revision", id }) if id == "current of deror")
        );
    }

    #[test]
    fn test_publish_write_failure_leaves_the_revision_frozen() {
        // Mirrors the freeze-failure leg at the publish gate: the error
        // surfaces and the published flag stays down.
        let mut rig = Fakes::rig();
        rig.fail_publish = true;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let error = block_on(service.publish("deror")).expect_err("publish fails");
        assert!(
            matches!(error, PublicationError::Failed(Error::Storage(message)) if message == "publish failed")
        );
        assert!(!fakes.lock().expect("lock").published);
    }

    #[test]
    fn test_publish_without_a_frozen_hash_seals_without_content() {
        // A revision with no stored hash publishes with a null content
        // summary: the hash only exists once a freeze stored it.
        let mut rig = Fakes::rig();
        rig.revision.as_mut().expect("revision").state = RevisionState::Frozen;
        let fakes = Mutex::new(rig);
        let service = service(&fakes);
        let sealed = block_on(service.publish("deror")).expect("publish");
        assert_eq!(sealed.state, "published");
        assert_eq!(sealed.content_hash, None);
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
        use marginalia_types::works::{
            BlockEntityLinkDraft, BlockSourceLinkDraft, WorkBlockDraft, WorkDraft,
            WorkRevisionDraft,
        };
        let fakes = Mutex::new(Fakes::rig());
        let shared = Shared(&fakes);
        let mut unit = ();
        let mut tx = FakeTx;
        let report =
            block_on(shared.validate_for_gate("deror", ValidateGate::Freeze, &HashSet::new()))
                .expect("validate");
        assert!(report.passed);
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
        assert!(block_on(WorkRepo::get(&shared, uuid(WORK_ID)))
            .expect("get")
            .is_none());
        assert_eq!(
            block_on(shared.get_by_slug("deror"))
                .expect("slug")
                .expect("work")
                .slug,
            "deror"
        );
        assert_eq!(block_on(shared.list()).expect("list").len(), 1);
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
        {
            let error = block_on(WorkRevisionRepo::insert(
                &shared,
                &mut tx,
                WorkRevisionDraft {
                    work_id: uuid(WORK_ID),
                    revision_number: 2,
                    parent_revision_id: None,
                    message: None,
                    created_by: "user".to_owned(),
                    metadata: serde_json::Map::new(),
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(WorkRevisionRepo::get(&shared, uuid(REV_ID)))
            .expect("get")
            .is_some());
        assert!(block_on(shared.latest(uuid(WORK_ID)))
            .expect("latest")
            .is_none());
        {
            let error = block_on(shared.copy_forward(&mut tx, uuid(REV_ID)))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.supersede(&mut tx, uuid(REV_ID)))
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
                    attributes: serde_json::Map::new(),
                },
                None,
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(shared.tree(uuid(REV_ID)))
            .expect("tree")
            .is_empty());
        assert!(block_on(WorkBlockRepo::get(&shared, uuid(REV_ID)))
            .expect("get")
            .is_none());
        assert!(
            block_on(WorkBlockRepo::by_key(&shared, uuid(REV_ID), uuid(REV_ID)))
                .expect("by_key")
                .is_none()
        );
        assert!(
            block_on(shared.by_key_in_tx(&mut unit, uuid(REV_ID), uuid(REV_ID)))
                .expect("by_key_in_tx")
                .is_none()
        );
        block_on(shared.delete(&mut unit, uuid(REV_ID))).expect("delete");
        {
            let error = block_on(shared.insert_occurrence(
                &mut unit,
                OccurrenceDraft {
                    block_id: uuid(REV_ID),
                    citation_key: Uuid::new_v4(),
                    placement: marginalia_types::works::Placement::Inline,
                    intent: marginalia_types::works_files::Intent::Quotation,
                    note: None,
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        {
            let error = block_on(shared.insert_item(
                &mut unit,
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
        assert!(block_on(CitationRepo::for_block(&shared, uuid(REV_ID)))
            .expect("for_block")
            .is_empty());
        assert!(block_on(CitationRepo::for_revision(&shared, uuid(REV_ID)))
            .expect("for_revision")
            .is_empty());
        assert!(
            block_on(CitationRepo::by_key(&shared, uuid(REV_ID), Uuid::new_v4()))
                .expect("by_key")
                .is_none()
        );
        assert!(block_on(shared.citing_span(Uuid::new_v4()))
            .expect("citing")
            .is_empty());
        assert!(block_on(shared.citing_key("DABAR_2026"))
            .expect("citing")
            .is_empty());
        {
            let error = block_on(shared.add_source_link(
                &mut unit,
                BlockSourceLinkDraft {
                    block_id: uuid(REV_ID),
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
            let error = block_on(shared.add_entity_link(
                &mut unit,
                BlockEntityLinkDraft {
                    block_id: uuid(REV_ID),
                    entity_id: Uuid::new_v4(),
                    relation: "renders".to_owned(),
                    surface_form: None,
                },
            ))
            .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        let links = block_on(WorkLinkRepo::for_block(&shared, uuid(REV_ID))).expect("for_block");
        assert!(links.sources.is_empty());
        assert!(links.entities.is_empty());
        assert!(block_on(shared.for_span(Uuid::new_v4()))
            .expect("for_span")
            .is_empty());
        assert!(block_on(shared.for_entity(Uuid::new_v4(), "renders"))
            .expect("for_entity")
            .is_empty());
        let mut span_tx = ();
        {
            let error = block_on(shared.resolve(&mut span_tx, uuid(REV_ID), 0, 4))
                .expect_err("unused writer refuses");
            assert!(matches!(error, Error::Storage(message) if message == "unused"));
        }
        assert!(block_on(SourceSpanRepo::get(&shared, uuid(REV_ID)))
            .expect("get")
            .is_none());
        assert!(block_on(shared.for_document(uuid(REV_ID)))
            .expect("for_document")
            .is_empty());
        assert!(block_on(shared.stale(10)).expect("stale").is_empty());
        let stored = block_on(WaiverRepo::insert(
            &shared,
            &mut tx,
            marginalia_types::works::WaiverDraft {
                revision_id: uuid(REV_ID),
                rule_id: "AUTH_QUOTE_UNVERIFIED".to_owned(),
                subject: None,
                actor: "user".to_owned(),
                reason: "reviewed".to_owned(),
            },
        ))
        .expect("insert waiver");
        assert_eq!(stored.rule_id, "AUTH_QUOTE_UNVERIFIED");
        assert_eq!(fakes.lock().expect("lock").waivers.len(), 1);
        assert!(block_on(WaiverRepo::for_revision(&shared, uuid(REV_ID)))
            .expect("for_revision")
            .is_empty());
        block_on(shared.begin()).expect("begin");
        block_on(shared.commit(FakeTx)).expect("commit");
        block_on(shared.rollback(FakeTx)).expect("rollback");
    }

    #[test]
    fn test_infrastructure_failures_surface_without_sealing() {
        // Every gate read and transaction boundary reports storage errors
        // as `Failed`: nothing is waived, frozen, or published on the way
        // out. Each case names the rig flag, the entry point, and the
        // message.
        fn rigged(flag: fn(&mut Fakes)) -> Mutex<Fakes> {
            let mut rig = Fakes::rig();
            flag(&mut rig);
            Mutex::new(rig)
        }
        type FailCase = (fn(&mut Fakes), &'static str);
        let freeze_cases: [FailCase; 6] = [
            (|rig| rig.fail_works = true, "works failed"),
            (|rig| rig.fail_validation = true, "validation failed"),
            (|rig| rig.fail_revisions = true, "revisions failed"),
            (|rig| rig.fail_tree = true, "tree failed"),
            (|rig| rig.fail_begin = true, "begin failed"),
            (|rig| rig.fail_commit = true, "commit failed"),
        ];
        for (flag, message) in freeze_cases {
            let fakes = rigged(flag);
            let service = service(&fakes);
            let error = block_on(service.freeze("deror", None, &[])).expect_err("infra fails");
            assert!(
                matches!(error, PublicationError::Failed(Error::Storage(failure)) if failure == message),
                "wrong failure for {message}"
            );
            let guard = fakes.lock().expect("lock");
            assert!(guard.waivers.is_empty());
            // The seal row is written before commit, so a commit failure is
            // the one case that leaves the stored hash behind.
            assert_eq!(guard.frozen_hash.is_some(), message == "commit failed");
        }
        let publish_cases: [FailCase; 4] = [
            (|rig| rig.fail_works = true, "works failed"),
            (|rig| rig.fail_validation = true, "validation failed"),
            (|rig| rig.fail_begin = true, "begin failed"),
            (|rig| rig.fail_commit = true, "commit failed"),
        ];
        for (flag, message) in publish_cases {
            let fakes = rigged(flag);
            let service = service(&fakes);
            let error = block_on(service.publish("deror")).expect_err("infra fails");
            assert!(
                matches!(error, PublicationError::Failed(Error::Storage(failure)) if failure == message),
                "wrong failure for {message}"
            );
            // The publish row is written before commit, so a commit failure
            // is the one case that leaves the published flag raised.
            assert_eq!(
                fakes.lock().expect("lock").published,
                message == "commit failed"
            );
        }
    }
}
