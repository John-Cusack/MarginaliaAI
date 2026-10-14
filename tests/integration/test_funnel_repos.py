"""Claim embeddings and pair scores round-trip against real Postgres.

Three claims about a caravan's toll go in through the real machine-claim
repo; the funnel repos then store their vectors and one NLI verdict. What
is under test is storage itself — the upsert, the ordered-pair convention,
the dedup, and that vector search ranks the nearest statement first.
Worked examples stay invented: caravans and tolls, never scripture.
"""

from __future__ import annotations

import math
import uuid
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from research_engine.adapters.profile_client import ProfileServiceAdapter
from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGEditionRepo,
    PGMachineClaimRepo,
    PGProfileRepo,
    PGRunRepo,
)
from research_engine.adapters.storage.postgres.repositories.funnel import (
    PGClaimEmbeddingRepo,
    PGPairScoreRepo,
)
from research_engine.domain.machine_claims import (
    ClaimEmbedding,
    MachineClaimDraft,
    Modality,
    PairRelation,
    PairScoreDraft,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MODEL = "test-embed"
MODEL_VERSION = "v1"
DIM = 1024


def _vec(*hot: int) -> list[float]:
    """A 1024-wide vector with 1.0 at the hot positions, else 0.0."""
    v = [0.0] * DIM
    for i in hot:
        v[i] = 1.0
    norm = math.sqrt(len(hot))
    return [x / norm for x in v]


def _draft(statement: str, run_id: UUID, document_id: UUID) -> MachineClaimDraft:
    return MachineClaimDraft(
        run_id=run_id,
        document_id=document_id,
        statement=statement,
        modality=Modality.asserts,
    )


def _verdict(a: UUID, b: UUID) -> PairScoreDraft:
    return PairScoreDraft(
        claim_a_id=a,
        claim_b_id=b,
        relation=PairRelation.contradict,
        confidence=0.9,
        rationale="The caravan cannot both have paid and not paid.",
        model="stub-nli",
        prompt_hash="p" * 16,
    )


@pytest.fixture
async def setup(engine: AsyncEngine, corpus: Corpus):
    profiles = ProfileServiceAdapter(
        editions=PGEditionRepo(engine),
        profiles=PGProfileRepo(engine),
        runs=PGRunRepo(engine),
        transaction_factory=partial(transaction, engine),
    )
    key = f"funnel_{uuid.uuid4().hex[:10]}"
    loaded = await profiles.load_profile(key, version=1, profile_hash="b" * 64, roles=[])
    corpus.adopt_profile(UUID(loaded["id"]))
    run = await profiles.start_run(key, stage="shortlist", config={})
    run_id = UUID(run["id"])

    document_id = await corpus.add_document(title="toll ledger")
    claims_repo = PGMachineClaimRepo(engine)
    statements = [
        "The caravan paid the toll at the northern gate.",
        "The caravan paid no toll at the northern gate.",
        "The caravan carried salt across the desert.",
    ]
    claim_ids = []
    async with transaction(engine) as tx:
        for statement in statements:
            stored = await claims_repo.insert_claim(tx, _draft(statement, run_id, document_id))
            assert stored is not None
            claim_ids.append(stored.id)
    return {
        "run_id": run_id,
        "claim_ids": claim_ids,
        "embeddings": PGClaimEmbeddingRepo(engine),
        "scores": PGPairScoreRepo(engine),
    }


async def test_vectors_roundtrip_and_nearest_ranks_first(engine: AsyncEngine, setup) -> None:
    a, b, c = setup["claim_ids"]
    repo = setup["embeddings"]
    async with transaction(engine) as tx:
        await repo.store(
            tx,
            [
                ClaimEmbedding(
                    claim_id=a,
                    model=MODEL,
                    model_version=MODEL_VERSION,
                    embedding=_vec(0),
                ),
                ClaimEmbedding(
                    claim_id=b,
                    model=MODEL,
                    model_version=MODEL_VERSION,
                    embedding=_vec(1),
                ),
                ClaimEmbedding(
                    claim_id=c,
                    model=MODEL,
                    model_version=MODEL_VERSION,
                    embedding=_vec(0, 1),
                ),
            ],
        )
    stored = await repo.get(a, MODEL, MODEL_VERSION)
    assert stored is not None
    assert stored.embedding == pytest.approx(_vec(0))

    hits = await repo.vector_search(_vec(0), MODEL, MODEL_VERSION, None, 3)
    assert [claim_id for claim_id, _ in hits] == [a, c, b]
    assert hits[0][1] == pytest.approx(1.0)

    # Candidate pushdown: the nearest claim is ineligible, so it never ranks.
    narrowed = await repo.vector_search(_vec(0), MODEL, MODEL_VERSION, [b, c], 3)
    assert [claim_id for claim_id, _ in narrowed] == [c, b]


async def test_reembedding_overwrites(engine: AsyncEngine, setup) -> None:
    (a, *_) = setup["claim_ids"]
    repo = setup["embeddings"]
    async with transaction(engine) as tx:
        await repo.store(
            tx,
            [
                ClaimEmbedding(
                    claim_id=a,
                    model=MODEL,
                    model_version=MODEL_VERSION,
                    embedding=_vec(0),
                )
            ],
        )
    async with transaction(engine) as tx:
        await repo.store(
            tx,
            [
                ClaimEmbedding(
                    claim_id=a,
                    model=MODEL,
                    model_version=MODEL_VERSION,
                    embedding=_vec(2),
                )
            ],
        )
    stored = await repo.get(a, MODEL, MODEL_VERSION)
    assert stored is not None
    assert stored.embedding == pytest.approx(_vec(2))


async def test_pairs_order_and_rescoring_is_a_no_op(setup) -> None:
    a, b, _c = setup["claim_ids"]
    repo = setup["scores"]
    run_id = setup["run_id"]
    # Deliberately unordered: the larger id first.
    hi, lo = (a, b) if str(a) > str(b) else (b, a)
    inserted = await repo.insert(run_id, [_verdict(hi, lo)])
    assert len(inserted) == 1
    assert (inserted[0].claim_a_id, inserted[0].claim_b_id) == (lo, hi)
    assert inserted[0].relation is PairRelation.contradict

    # Re-scoring the same pair, now in order, writes nothing new.
    assert await repo.insert(run_id, [_verdict(lo, hi)]) == []
    scores = await repo.for_run(run_id)
    assert len(scores) == 1
    assert scores[0].confidence == pytest.approx(0.9)
