"""Shortlisting eligible claims into candidate pairs, without a database.

The shortlister talks to two narrow collaborators, so scripted fakes
answer them: an embedder that records the statements it was asked to
embed, and a claim-embeddings repo that either scores cosine similarity
over stored vectors or replays canned neighbours. What is under test is
what the shortlister itself decides — which statements get embedded,
which ids reach the vector search, and how raw hits become deduped
ordered pairs. Storage itself belongs to the integration suite.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from math import sqrt
from uuid import UUID, uuid4

import pytest

from research_engine.domain.machine_claims import (
    ClaimEmbedding,
    MachineClaim,
    Modality,
)
from research_engine.services.dilemma.shortlist import ClaimShortlister

pytestmark = pytest.mark.unit

RUN_ID = uuid4()
MODEL = "bge-m3"
MODEL_VERSION = "test-v1"


def _claim(statement: str, claim_id: UUID | None = None) -> MachineClaim:
    return MachineClaim(
        id=claim_id or uuid4(),
        run_id=RUN_ID,
        document_id=uuid4(),
        statement=statement,
        modality=Modality.asserts,
        created_at=datetime.now(UTC),
    )


@asynccontextmanager
async def _transaction():
    yield object()


class _Embedder:
    """Returns one scripted vector per text; records what it was asked for."""

    def __init__(self, vectors: list[list[float]]):
        self._vectors = list(vectors)
        self.texts: list[str] = []
        self.calls = 0

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts.extend(texts)
        return [self._vectors[i % len(self._vectors)] for i in range(len(texts))]


class _CosineEmbeddings:
    """Dict-backed store scoring cosine similarity over stored vectors."""

    def __init__(self, stored: dict[UUID, list[float]] | None = None):
        self.vectors: dict[UUID, list[float]] = dict(stored or {})
        self.stored_rows: list[ClaimEmbedding] = []
        self.searched: list[list[UUID] | None] = []

    async def get(
        self, claim_id: UUID, model: str, version: str
    ) -> ClaimEmbedding | None:
        assert (model, version) == (MODEL, MODEL_VERSION)
        vec = self.vectors.get(claim_id)
        if vec is None:
            return None
        return ClaimEmbedding(
            claim_id=claim_id,
            model=model,
            model_version=version,
            embedding=vec,
        )

    async def store(self, tx, rows: list[ClaimEmbedding]) -> None:
        self.stored_rows.extend(rows)
        for row in rows:
            assert isinstance(row, ClaimEmbedding)
            self.vectors[row.claim_id] = row.embedding

    async def vector_search(
        self,
        query_vec: list[float],
        model: str,
        version: str,
        candidate_ids: list[UUID] | None,
        k: int,
    ) -> list[tuple[UUID, float]]:
        self.searched.append(candidate_ids)
        scored = [
            (cid, _cosine(query_vec, vec))
            for cid, vec in self.vectors.items()
            if candidate_ids is None or cid in set(candidate_ids)
        ]
        scored.sort(key=lambda hit: hit[1], reverse=True)
        return scored[:k]


class _ScriptedEmbeddings(_CosineEmbeddings):
    """Replays canned neighbours instead of scoring stored vectors."""

    def __init__(
        self,
        stored: dict[UUID, list[float]],
        neighbours: dict[UUID, list[tuple[UUID, float]]],
    ):
        super().__init__(stored)
        self._neighbours = neighbours
        self._by_vec = {tuple(vec): cid for cid, vec in stored.items()}

    async def vector_search(
        self,
        query_vec: list[float],
        model: str,
        version: str,
        candidate_ids: list[UUID] | None,
        k: int,
    ) -> list[tuple[UUID, float]]:
        self.searched.append(candidate_ids)
        asker = self._by_vec[tuple(query_vec)]
        allowed = set(candidate_ids) if candidate_ids is not None else None
        hits = [
            (cid, score)
            for cid, score in self._neighbours[asker]
            if allowed is None or cid in allowed
        ]
        return hits[:k]


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return dot / norm if norm else 0.0


def _vecs(*rows: tuple[float, ...]) -> list[list[float]]:
    return [list(row) for row in rows]


async def test_stored_embeddings_are_reused_without_reembedding() -> None:
    first, second = _claim("The caravan paid no toll."), _claim(
        "The caravan paid a toll at the gate."
    )
    stored = {first.id: [1.0, 0.0], second.id: [0.9, 0.1]}
    embedder = _Embedder(_vecs((0.0, 1.0)))
    embeddings = _CosineEmbeddings(stored)
    shortlister = ClaimShortlister(embedder, embeddings, _transaction)

    pairs = await shortlister.shortlist(
        [first, second], {first.id, second.id}, 1, MODEL, MODEL_VERSION
    )

    assert embedder.calls == 0
    assert embeddings.stored_rows == []
    a, b = _ordered(first.id, second.id)
    assert pairs == [(a, b, _cosine(stored[first.id], stored[second.id]))]


async def test_missing_statements_are_embedded_once_and_stored() -> None:
    first, second = _claim("The caravan paid no toll."), _claim(
        "The caravan carried salt."
    )
    outsider = _claim("The gate stands open at night.")
    fresh = _vecs((1.0, 0.0), (0.0, 1.0))
    embedder = _Embedder(fresh)
    embeddings = _CosineEmbeddings({first.id: [1.0, 0.0]})
    shortlister = ClaimShortlister(embedder, embeddings, _transaction)

    pairs = await shortlister.shortlist(
        [first, second, outsider],
        {first.id, second.id},
        1,
        MODEL,
        MODEL_VERSION,
    )

    assert embedder.calls == 1
    assert embedder.texts == [second.statement]
    assert embeddings.stored_rows == [
        ClaimEmbedding(
            claim_id=second.id,
            model=MODEL,
            model_version=MODEL_VERSION,
            embedding=fresh[0],
        )
    ]
    assert embeddings.vectors[second.id] == fresh[0]
    assert all(outsider.id not in (a, b) for a, b, _ in pairs)


async def test_pairs_are_top_k_deduped_ordered_and_self_free() -> None:
    first, second, third = (
        _claim("The caravan paid no toll."),
        _claim("The caravan paid a toll at the gate."),
        _claim("The caravan crossed by daylight."),
    )
    stored = {first.id: [1.0, 0.0], second.id: [0.0, 1.0], third.id: [0.0, 0.5]}
    neighbours = {
        first.id: [(second.id, 0.9), (third.id, 0.5)],
        second.id: [(first.id, 0.9), (third.id, 0.4)],
        third.id: [(first.id, 0.85), (second.id, 0.4)],
    }
    shortlister = ClaimShortlister(
        _Embedder(_vecs((0.0, 0.0))),
        _ScriptedEmbeddings(stored, neighbours),
        _transaction,
    )
    claims = [first, second, third]
    eligible = {first.id, second.id, third.id}

    one_each = await shortlister.shortlist(claims, eligible, 1, MODEL, MODEL_VERSION)
    a, b = _ordered(first.id, second.id)
    c, d = _ordered(first.id, third.id)
    assert one_each == sorted(
        [(a, b, 0.9), (c, d, 0.85)], key=lambda p: (str(p[0]), str(p[1]))
    )
    two_each = await shortlister.shortlist(claims, eligible, 2, MODEL, MODEL_VERSION)
    assert len(two_each) == 3
    for a, b, _ in two_each:
        assert a != b
        assert str(a) < str(b)
    assert [(a, b) for a, b, _ in two_each] == sorted(
        [(a, b) for a, b, _ in two_each], key=lambda p: (str(p[0]), str(p[1]))
    )


async def test_search_candidates_stay_inside_the_eligible_set() -> None:
    first, second = _claim("The caravan paid no toll."), _claim(
        "The caravan paid a toll at the gate."
    )
    outsider = _claim("The gate stands open at night.")
    stored = {first.id: [1.0, 0.0], second.id: [0.0, 1.0], outsider.id: [0.5, 0.5]}
    neighbours = {
        first.id: [(second.id, 0.7), (outsider.id, 0.99)],
        second.id: [(first.id, 0.7), (outsider.id, 0.99)],
    }
    embeddings = _ScriptedEmbeddings(stored, neighbours)
    shortlister = ClaimShortlister(
        _Embedder(_vecs((0.0, 0.0))), embeddings, _transaction
    )
    eligible = {first.id, second.id}

    pairs = await shortlister.shortlist(
        [first, second, outsider], eligible, 2, MODEL, MODEL_VERSION
    )

    assert embeddings.searched and all(
        set(candidates) <= eligible for candidates in embeddings.searched
    )
    for candidates, claim in zip(embeddings.searched, [first, second], strict=True):
        assert claim.id not in candidates
    assert pairs == [
        (_ordered(first.id, second.id)[0], _ordered(first.id, second.id)[1], 0.7)
    ]


def _ordered(first: UUID, second: UUID) -> tuple[UUID, UUID]:
    if str(first) < str(second):
        return (first, second)
    return (second, first)
