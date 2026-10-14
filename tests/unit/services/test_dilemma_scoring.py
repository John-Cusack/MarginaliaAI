"""Judging shortlisted pairs, without a database.

`PairScorer` judges each pair with one structured call and inserts every
judgment, so fakes stand in for the LLM and the pair-scores repo. What is
under test is the funnel behaviour — only contradicting pairs at or above
threshold come back, scored pairs cost no second call, a rejected answer
gets one corrective retry, and each kept draft carries its LLM call id —
not the SQL, which the integration suite exercises against a real database.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research_engine.services.dilemma.scoring import PairScorer

pytestmark = pytest.mark.unit

RUN_ID = uuid4()
MODEL = "test-nli-1"
PROMPT_VERSION = "v1"
TAU = 0.5


def _texts(statement_a="The caravan paid the gate toll."):
    a, b, c, d, e, f = (uuid4() for _ in range(6))
    return {
        a: (statement_a, "asserts", "affirms"),
        b: ("The caravan paid no toll at the gate.", "asserts", "affirms"),
        c: ("The ferry runs after dark.", "asserts", "affirms"),
        d: ("The ferry never runs after dark.", "asserts", "affirms"),
        e: ("The toll keeper naps at noon.", "asserts", "affirms"),
        f: ("The keeper dozes at midday.", "asserts", "affirms"),
    }


class FakeLLM:
    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls = []

    async def structured(
        self, messages, schema, model=None, caller="core", purpose=""
    ):
        prompt = messages[0]["content"]
        self.calls.append(
            {"purpose": purpose, "prompt": prompt, "model": model}
        )
        assert self._answers, f"unexpected LLM call: {prompt!r}"
        answer = self._answers.pop(0)
        call_id = uuid4()
        self.calls[-1]["call_id"] = call_id
        return answer, call_id

    async def complete(self, *a, **kw):  # pragma: no cover - unused here
        raise NotImplementedError


class FakePairScores:
    """Already-stored rows plus every draft the scorer inserted."""

    def __init__(self, existing=()):
        self.existing = list(existing)
        self.inserted = []

    async def for_run(self, run_id):
        return [row for row in self.existing if row.run_id == run_id]

    async def insert(self, run_id, rows):
        self.inserted.extend(rows)


def _answer(relation, confidence, rationale="One sentence."):
    return {
        "relation": relation,
        "confidence": confidence,
        "rationale": rationale,
    }


def _row(run_id, a, b, relation, confidence, prompt_hash, call_id=None):
    return SimpleNamespace(
        run_id=run_id,
        claim_a_id=a,
        claim_b_id=b,
        relation=relation,
        confidence=confidence,
        rationale="Stored reason.",
        llm_call_id=call_id or uuid4(),
        model=MODEL,
        prompt_hash=prompt_hash,
    )


async def test_only_contradictions_at_threshold_come_back() -> None:
    texts = _texts()
    ids = list(texts)
    pairs = [(ids[0], ids[1]), (ids[2], ids[3]), (ids[4], ids[5])]
    llm = FakeLLM(
        _answer("contradict", 0.9),
        _answer("contradict", 0.4),
        _answer("entail", 0.95),
    )
    repo = FakePairScores()
    scorer = PairScorer(llm, repo)

    kept = await scorer.score_pairs(
        RUN_ID, pairs, texts, TAU, MODEL, PROMPT_VERSION
    )

    expected = tuple(sorted(pairs[0], key=str))
    assert [(d.claim_a_id, d.claim_b_id) for d in kept] == [expected]
    assert kept[0].relation == "contradict"
    # Every judgment is stored, even the ones the funnel drops.
    assert len(repo.inserted) == 3
    assert {d.relation for d in repo.inserted} == {
        "contradict",
        "entail",
    }


async def test_a_scored_pair_makes_no_second_call() -> None:
    texts = _texts()
    ids = list(texts)
    pairs = [(ids[0], ids[1])]
    first_llm = FakeLLM(_answer("contradict", 0.9))
    first_repo = FakePairScores()
    scorer = PairScorer(first_llm, first_repo)
    first_kept = await scorer.score_pairs(
        RUN_ID, pairs, texts, TAU, MODEL, PROMPT_VERSION
    )

    second_llm = FakeLLM()
    second_repo = FakePairScores(existing=first_repo.inserted)
    rerun = PairScorer(second_llm, second_repo)
    second_kept = await rerun.score_pairs(
        RUN_ID, pairs, texts, TAU, MODEL, PROMPT_VERSION
    )

    assert second_llm.calls == []
    assert second_repo.inserted == []
    assert [d.llm_call_id for d in second_kept] == [
        d.llm_call_id for d in first_kept
    ]


async def test_rejected_answer_gets_one_retry_then_keeps() -> None:
    texts = _texts()
    ids = list(texts)
    llm = FakeLLM(
        {"relation": "maybe", "confidence": 0.9, "rationale": "Bad label."},
        _answer("contradict", 0.8),
    )
    repo = FakePairScores()
    scorer = PairScorer(llm, repo)

    kept = await scorer.score_pairs(
        RUN_ID, [(ids[0], ids[1])], texts, TAU, MODEL, PROMPT_VERSION
    )

    assert [call["purpose"] for call in llm.calls] == [
        "nli_scoring",
        "nli_scoring_retry",
    ]
    assert len(kept) == 1
    assert kept[0].relation == "contradict"
    assert kept[0].llm_call_id == llm.calls[1]["call_id"]


async def test_kept_draft_carries_call_id_hash_and_order() -> None:
    texts = _texts()
    ids = list(texts)
    low, high = sorted(ids[:2], key=str)
    llm = FakeLLM(_answer("contradict", 0.9))
    repo = FakePairScores()
    scorer = PairScorer(llm, repo)

    kept = await scorer.score_pairs(
        RUN_ID, [(high, low)], texts, TAU, MODEL, PROMPT_VERSION
    )

    (draft,) = kept
    assert (draft.claim_a_id, draft.claim_b_id) == (low, high)
    assert draft.llm_call_id == llm.calls[0]["call_id"]
    assert draft.model == MODEL
    expected_hash = hashlib.sha256(
        llm.calls[0]["prompt"].encode()
    ).hexdigest()
    assert draft.prompt_hash == expected_hash
    assert texts[low][0] in llm.calls[0]["prompt"]
    assert texts[high][0] in llm.calls[0]["prompt"]
