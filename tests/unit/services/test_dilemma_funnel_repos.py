"""Funnel repositories' SQL shape, without a database.

The repos talk to one narrow collaborator — an engine handing out
connections — so a fake engine records the statements they build. What is
under test is the SQL shape the database will see: pairs ordered
(``a < b``) with ``ON CONFLICT DO NOTHING``, embeddings upserted by key,
and vector search pushed down to the eligible candidates. Real storage
belongs to the integration suite.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from research_engine.adapters.storage.postgres.repositories.funnel import (
    PGClaimEmbeddingRepo,
    PGPairScoreRepo,
    ordered_pair,
)
from research_engine.domain.machine_claims import (
    ClaimEmbedding,
    PairRelation,
    PairScoreDraft,
)

pytestmark = pytest.mark.unit

MODEL = "bge-m3"
VERSION = "1.0"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _Conn:
    def __init__(self, rows=None):
        self.statements = []
        self._rows = rows or []

    async def execute(self, stmt, params=None):
        self.statements.append((stmt, params))
        return _Result(self._rows)


class _Engine:
    """Hands out one scripted connection; records nothing itself."""

    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def begin(self):
        yield self._conn

    @asynccontextmanager
    async def connect(self):
        yield self._conn


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _row(claim_id: UUID) -> ClaimEmbedding:
    return ClaimEmbedding(
        claim_id=claim_id,
        model=MODEL,
        model_version=VERSION,
        embedding=[0.1, 0.2, 0.3],
    )


def _draft(a: UUID, b: UUID) -> PairScoreDraft:
    return PairScoreDraft(
        claim_a_id=a,
        claim_b_id=b,
        relation=PairRelation.contradict,
        confidence=0.9,
        rationale="The caravan either paid or it did not.",
        model="stub-nli",
        prompt_hash="h" * 16,
    )


def test_pairs_order_below_by_string_compare() -> None:
    a, b = uuid4(), uuid4()
    lo, hi = (a, b) if str(a) < str(b) else (b, a)
    assert ordered_pair(a, b) == (lo, hi)
    assert ordered_pair(b, a) == (lo, hi)


async def test_pair_insert_orders_and_ignores_repeats() -> None:
    conn = _Conn()
    repo = PGPairScoreRepo(_Engine(conn))
    # Deliberately unordered: the larger id first.
    a, b = uuid4(), uuid4()
    hi, lo = (a, b) if str(a) > str(b) else (b, a)
    await repo.insert(uuid4(), [_draft(hi, lo)])
    assert len(conn.statements) == 1
    sql = _compiled(conn.statements[0][0])
    assert "dilemma.pair_scores" in sql
    assert "ON CONFLICT ON CONSTRAINT pair_scores_pkey DO NOTHING" in sql
    # The stored pair is ordered, whatever order the draft carried.
    assert sql.index(str(lo)) < sql.index(str(hi))


async def test_embedding_store_upserts_by_key() -> None:
    conn = _Conn()
    await PGClaimEmbeddingRepo(_Engine(conn)).store(SimpleNamespace(conn=conn), [_row(uuid4())])
    assert len(conn.statements) == 1
    # The vector literal cannot render with literal binds, so read the
    # parametrized shape: table, key, and the rewrite clause.
    sql = str(conn.statements[0][0])
    assert "dilemma.claim_embeddings" in sql
    assert "ON CONFLICT ON CONSTRAINT claim_embeddings_pkey DO UPDATE" in sql
    assert "embedding" in sql


async def test_embedding_store_empty_stores_nothing() -> None:
    conn = _Conn()
    await PGClaimEmbeddingRepo(_Engine(conn)).store(SimpleNamespace(conn=conn), [])
    assert conn.statements == []


async def test_vector_search_pushes_candidates_down() -> None:
    conn = _Conn()
    hits = await PGClaimEmbeddingRepo(_Engine(conn)).vector_search(
        [0.1, 0.2, 0.3], MODEL, VERSION, [uuid4(), uuid4()], 5
    )
    assert hits == []
    stmt, params = conn.statements[0]
    sql = str(stmt)
    assert "dilemma.claim_embeddings" in sql
    assert params["no_filter"] is False
    assert params["k"] == 5


async def test_vector_search_without_candidates_reads_everything() -> None:
    conn = _Conn()
    await PGClaimEmbeddingRepo(_Engine(conn)).vector_search(
        [0.1, 0.2, 0.3], MODEL, VERSION, None, 5
    )
    assert conn.statements[0][1]["no_filter"] is True


async def test_for_run_deserializes_verdicts() -> None:
    run_id = uuid4()
    a, b = uuid4(), uuid4()
    lo, hi = (a, b) if str(a) < str(b) else (b, a)
    conn = _Conn(
        rows=[
            SimpleNamespace(
                _mapping={
                    "run_id": run_id,
                    "claim_a_id": lo,
                    "claim_b_id": hi,
                    "relation": "contradict",
                    "confidence": 0.9,
                    "rationale": "The caravan either paid or it did not.",
                    "llm_call_id": None,
                    "model": "stub-nli",
                    "prompt_hash": "h" * 16,
                    "created_at": datetime.now(UTC),
                }
            )
        ]
    )
    scores = await PGPairScoreRepo(_Engine(conn)).for_run(run_id)
    assert len(scores) == 1
    assert scores[0].relation is PairRelation.contradict


def test_pair_draft_rejects_out_of_range_confidence() -> None:
    from pydantic import ValidationError

    data = _draft(uuid4(), uuid4()).model_dump()
    with pytest.raises(ValidationError):
        PairScoreDraft(**{**data, "confidence": 1.5})
