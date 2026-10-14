"""Outside NLI verdicts: shapes in, drafts out, no database.

`_as_judgment` is the import's contract with whoever judged the pairs — a
Muse agent, a batch job. What is under test is which answers count as
verdicts; storing them belongs to the integration suite.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from research_engine.cli.dilemma import _as_judgment, _JudgmentFailure

pytestmark = pytest.mark.unit
FIRST = uuid4()
SECOND = uuid4()
PAIRS = {f"{FIRST}:{SECOND}": (FIRST, SECOND)}


def _answer(**overrides):
    record = {
        "pair_id": f"{FIRST}:{SECOND}",
        "relation": "contradict",
        "confidence": 0.9,
        "rationale": "Both cannot hold: one affirms the toll, the other denies it.",
        "model": "muse-agent:muse-wonder-full-1.0",
        "prompt_hash": "abc",
    }
    record.update(overrides)
    return record


def test_a_good_verdict_maps_to_a_draft() -> None:
    draft = _as_judgment(PAIRS, _answer())

    assert draft.claim_a_id == FIRST
    assert draft.claim_b_id == SECOND
    assert draft.relation.value == "contradict"
    assert draft.confidence == 0.9
    assert draft.llm_call_id is None


def test_an_unknown_pair_id_is_refused() -> None:
    with pytest.raises(_JudgmentFailure):
        _as_judgment(PAIRS, _answer(pair_id="nope"))


def test_a_bad_relation_is_refused() -> None:
    with pytest.raises(_JudgmentFailure):
        _as_judgment(PAIRS, _answer(relation="maybe"))


def test_confidence_must_be_a_number_in_range() -> None:
    with pytest.raises(_JudgmentFailure):
        _as_judgment(PAIRS, _answer(confidence=1.5))
    with pytest.raises(_JudgmentFailure):
        _as_judgment(PAIRS, _answer(confidence="high"))


def test_an_empty_rationale_is_refused() -> None:
    with pytest.raises(_JudgmentFailure):
        _as_judgment(PAIRS, _answer(rationale="  "))
