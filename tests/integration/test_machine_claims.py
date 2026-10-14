"""Machine claims materialize from extraction records, against real Postgres.

The path under test is the dilemma import path end to end minus the model:
records arrive through the executor's `import_records` (validated and stored
exactly as a model run's would be), and the materializer turns them into
claims whose evidence is re-verified against the document's canonical text.
A canonical text that disagrees with the passage is the failure case: the
span the resolver stores quotes the canonical slice, so the claim must be
refused rather than anchored to words the passage does not contain.
"""

from __future__ import annotations

import uuid
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from research_engine.adapters.profile_client import ProfileServiceAdapter
from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGDocumentRepo,
    PGDocumentTextRepo,
    PGEditionRepo,
    PGEntityRepo,
    PGExtractionRepo,
    PGExtractionSchemaRepo,
    PGMachineClaimRepo,
    PGPassageRepo,
    PGProfileRepo,
    PGRunRepo,
    PGSourceSpanRepo,
)
from research_engine.adapters.storage.postgres.schema import extraction_schemas
from research_engine.domain.machine_claims import VouchKind
from research_engine.services.dilemma.materialize import ClaimMaterializer
from research_engine.services.extraction.executor import ExtractionExecutor
from research_engine.services.extraction.registration import ExtractionSchemaService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TEXT = (
    "The elder said the river gives life to the valley. "
    "The scroll is faithful and true in all it reports."
)

SCHEMA_YAML = """
id: machine_claims_roundtrip
version: 1
owner: test_machine_claims
record_types:
  - id: claim
    fields:
      statement: {type: string, required: true}
      subject: {type: string}
      subject_ref: {type: string}
      predicate: {type: string}
      object: {type: string}
      polarity: {type: enum, values: [affirms, denies], required: true}
      modality:
        type: enum
        values: [asserts, commands, prophesies, reports_other_view,
                 quotes_scripture, hypothetical, question]
        required: true
      speaker: {type: string}
      speaker_ref: {type: string}
      scope_quantifier: {type: string}
      temporal_scope: {type: string}
      topic_keys: {type: array}
      evidence: {type: evidence_span, required: true}
      evidence_2: {type: evidence_span}
      confidence: {type: number, required: true}
  - id: vouch
    fields:
      statement: {type: string, required: true}
      kind:
        type: enum
        values: [affirms_reliability, affirms_authority, claims_prophesied_by,
                 self_reliability, affirms_fallibility]
        required: true
      target: {type: string}
      target_ref: {type: string}
      target_form: {type: enum, values: [work_as_given, extant_edition, unspecified]}
      speaker: {type: string}
      speaker_ref: {type: string}
      modality:
        type: enum
        values: [asserts, commands, prophesies, reports_other_view,
                 quotes_scripture, hypothetical, question]
        required: true
      evidence: {type: evidence_span, required: true}
      evidence_2: {type: evidence_span}
      confidence: {type: number, required: true}
prompt: |
  Extract claims from this passage.

  {{ passage_text }}
"""

RECORDS = [
    {
        "record_type": "claim",
        "fields": {
            "statement": "The river gives life to the valley",
            "subject": "the river",
            "predicate": "gives life",
            "object": "the valley",
            "polarity": "affirms",
            "modality": "asserts",
            "speaker": "the elder",
            "topic_keys": ["river"],
            "evidence": "the river gives life to the valley",
            "evidence_2": "The elder said the river gives life",
            "confidence": 0.9,
        },
    },
    {
        "record_type": "vouch",
        "fields": {
            "statement": "The scroll is faithful and true",
            "kind": "affirms_reliability",
            "target": "the scroll",
            "target_form": "unspecified",
            "speaker": "the elder",
            "modality": "asserts",
            "evidence": "The scroll is faithful and true",
            "confidence": 0.8,
        },
    },
]


class _NoLLM:
    """Import never calls a model; being asked is a test failure."""

    async def structured(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("import_records must not call a model")

    async def complete(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("import_records must not call a model")


@pytest.fixture
async def setup(engine: AsyncEngine, corpus: Corpus):
    schema_repo = PGExtractionSchemaRepo(engine)
    service = ExtractionSchemaService(
        schema_repo, lambda: transaction(engine)
    )
    schema = await service.register_yaml(SCHEMA_YAML)
    corpus.track(extraction_schemas, schema.id)

    profiles = ProfileServiceAdapter(
        editions=PGEditionRepo(engine),
        profiles=PGProfileRepo(engine),
        runs=PGRunRepo(engine),
        transaction_factory=partial(transaction, engine),
    )
    key = f"test_{uuid.uuid4().hex[:10]}"
    loaded = await profiles.load_profile(
        key, version=1, profile_hash="a" * 64, roles=[]
    )
    corpus.adopt_profile(UUID(loaded["id"]))
    run = await profiles.start_run(key, stage="extraction", config={})
    run_id = UUID(run["id"])

    executor = ExtractionExecutor(
        llm=_NoLLM(),
        passages=PGPassageRepo(engine),
        extractions=PGExtractionRepo(engine),
        extraction_schemas=schema_repo,
        transaction_factory=lambda: transaction(engine),
        default_model="stub-model",
    )
    claims_repo = PGMachineClaimRepo(engine)
    materializer = ClaimMaterializer(
        PGExtractionRepo(engine),
        schema_repo,
        PGPassageRepo(engine),
        PGSourceSpanRepo(engine),
        claims_repo,
        PGEntityRepo(engine),
        lambda: transaction(engine),
        documents=PGDocumentRepo(engine),
    )

    async def add_passage(passage_text: str, canonical_text: str) -> UUID:
        document_id = await corpus.add_document(title="roundtrip")
        async with transaction(engine) as tx:
            await PGDocumentTextRepo(engine).put(
                tx, document_id, canonical_text, "test", "1.0"
            )
        return await corpus.add_passage(document_id, passage_text)

    return {
        "run_id": run_id,
        "executor": executor,
        "materializer": materializer,
        "claims": claims_repo,
        "spans": PGSourceSpanRepo(engine),
        "add_passage": add_passage,
    }


async def _import_and_materialize(setup, passage_id: UUID):
    batch = await setup["executor"].import_records(
        {passage_id: RECORDS},
        "machine_claims_roundtrip:1",
        model="stub-model",
    )
    assert [r.status.value for r in batch.results] == ["ok"]
    return await setup["materializer"].materialize(
        run_id=setup["run_id"], schema_ref="machine_claims_roundtrip:1"
    )


async def test_claim_and_vouch_records_materialize_with_ordered_evidence(
    engine: AsyncEngine, corpus: Corpus, setup
) -> None:
    passage_id = await setup["add_passage"](TEXT, TEXT)

    stats = await _import_and_materialize(setup, passage_id)

    assert stats == {
        "records": 2,
        "claims_created": 2,
        "vouches_created": 1,
        "skipped_existing": 0,
        "skipped_no_offsets": 0,
        "evidence_failures": 0,
        "bundled_flagged": 0,
    }
    claims = await setup["claims"].for_run(setup["run_id"])
    assert len(claims) == 2
    for claim in claims:
        for row in await setup["claims"].evidence_for_claim(claim.id):
            span = await setup["spans"].get(row.source_span_id)
            corpus.adopt_span(span.id)

    claim = next(c for c in claims if c.predicate == "gives life")
    assert claim.subject_label == "the river"
    assert claim.object_label == "the valley"
    assert claim.speaker_label == "the elder"
    assert claim.speaker_entity_id is None  # labelled, never invented
    assert claim.topic_keys == ["river"]

    evidence = await setup["claims"].evidence_for_claim(claim.id)
    assert [(row.field, row.ordinal) for row in evidence] == [
        ("evidence", 0),
        ("evidence_2", 1),
    ]
    quoted = [
        (await setup["spans"].get(row.source_span_id)).quoted_text
        for row in evidence
    ]
    assert quoted == [
        "the river gives life to the valley",
        "The elder said the river gives life",
    ]

    [vouch] = await setup["claims"].vouches_for_run(setup["run_id"])
    assert vouch.kind is VouchKind.affirms_reliability
    assert vouch.target_label == "the scroll"
    vouched = next(c for c in claims if c.id == vouch.claim_id)
    assert vouched.statement == "The scroll is faithful and true"
    assert vouched.predicate is None


async def test_rematerializing_is_a_no_op(
    engine: AsyncEngine, corpus: Corpus, setup
) -> None:
    passage_id = await setup["add_passage"](TEXT, TEXT)
    await _import_and_materialize(setup, passage_id)
    for claim in await setup["claims"].for_run(setup["run_id"]):
        for row in await setup["claims"].evidence_for_claim(claim.id):
            corpus.adopt_span(row.source_span_id)

    again = await setup["materializer"].materialize(
        run_id=setup["run_id"], schema_ref="machine_claims_roundtrip:1"
    )

    assert again["claims_created"] == 0
    assert again["vouches_created"] == 0
    assert again["skipped_existing"] == 2
    assert len(await setup["claims"].for_run(setup["run_id"])) == 2
    assert len(await setup["claims"].vouches_for_run(setup["run_id"])) == 1


async def test_evidence_that_disagrees_with_the_canonical_text_is_refused(
    corpus: Corpus, setup
) -> None:
    """The passage says one thing; the document's canonical text another.

    Offsets address the canonical text, so the resolver's stored slice cannot
    equal the passage slice — and no claim may be anchored on the difference.
    """
    canonical = TEXT.replace("gives life", "gives XXXX")
    passage_id = await setup["add_passage"](TEXT, canonical)

    stats = await _import_and_materialize(setup, passage_id)

    # The claim's evidence crosses the altered words; the vouch's does not.
    assert stats["evidence_failures"] == 1
    assert stats["claims_created"] == 1
    claims = await setup["claims"].for_run(setup["run_id"])
    assert [c.statement for c in claims] == ["The scroll is faithful and true"]
    for row in await setup["claims"].evidence_for_claim(claims[0].id):
        corpus.adopt_span(row.source_span_id)
