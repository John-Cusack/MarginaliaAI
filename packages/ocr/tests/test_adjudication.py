"""Adjudication: a difference is a detection only after a second look.

The rules this file pins are the operator's, and each of them is a way the step
could silently inflate the audit's sensitivity — its own measure. The order the
two readings are shown in must not decide the answer; "unsure", a skipped
verdict, or a call that failed must all count as *not detected*; and one screen's
disputes must travel in one call.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import claude_adjudicator as adjudicator  # noqa: E402


def test_the_shown_order_is_random_and_the_side_still_tracks_the_audit():
    places = [((0, 5), "MARGIN", "Margin")] * 40
    rng = random.Random(7)
    disputes = adjudicator.disputes_for(places, rng=rng)
    sides = {dispute.auditor_side for dispute in disputes}
    assert sides == {"a", "b"}, "a fixed order would let the adjudicator learn the audit's position"
    for dispute in disputes:
        shown = dispute.first if dispute.auditor_side == "a" else dispute.second
        assert shown == "Margin", "the side must always point at the audit pass's own reading"


def test_an_empty_side_is_not_a_dispute():
    disputes = adjudicator.disputes_for([((0, 5), "", "Margin")], rng=random.Random(1))
    assert disputes == []


def test_side_picks_are_confirmed_and_unsure_or_silence_is_not():
    rng = random.Random(11)
    disputes = adjudicator.disputes_for(
        [((0, 5), "MARGIN", "Margin"), ((10, 15), "debt", "debr"), ((20, 25), "1099", "1999")],
        rng=rng,
    )
    # The adjudicator answers "a" for every dispute: only those whose audit
    # reading happens to be on side "a" can be confirmations. With the shuffle
    # seeded, both sides occur, so that is not the whole set.
    verdicts = tuple(adjudicator.Verdict(index=dispute.index, choice="a") for dispute in disputes)
    confirmed = adjudicator.confirmed_spans(disputes, verdicts)
    assert {dispute.auditor_side for dispute in disputes} == {"a", "b"}
    assert confirmed == {dispute.span for dispute in disputes if dispute.auditor_side == "a"}
    assert confirmed != {dispute.span for dispute in disputes}

    unsure = tuple(
        adjudicator.Verdict(index=dispute.index, choice="unsure") for dispute in disputes
    )
    assert adjudicator.confirmed_spans(disputes, unsure) == set()

    # A verdict the model never gave must not become agreement.
    partial = tuple(
        adjudicator.Verdict(index=dispute.index, choice=dispute.auditor_side)
        for dispute in disputes[:-1]
    )
    assert len(adjudicator.confirmed_spans(disputes[:2], partial)) == 2
    assert adjudicator.confirmed_spans(disputes, partial) == {
        dispute.span for dispute in disputes[:-1]
    }


def test_validate_refuses_an_index_that_is_not_a_dispute():
    with pytest.raises(adjudicator.ResponseRejected):
        adjudicator.validate({"verdicts": [{"index": 9, "choice": "a"}]}, 3)
    with pytest.raises(adjudicator.ResponseRejected):
        adjudicator.validate({"verdicts": [{"index": 0, "choice": "maybe"}]}, 3)
    with pytest.raises(adjudicator.ResponseRejected):
        adjudicator.validate({"nope": []}, 3)


def test_validate_fills_a_skipped_dispute_with_unsure():
    verdicts = adjudicator.validate({"verdicts": [{"index": 1, "choice": "b"}]}, 3)
    assert [verdict.choice for verdict in verdicts] == ["unsure", "b", "unsure"]


def test_render_disputes_shows_both_readings_without_saying_which_is_which():
    disputes = adjudicator.disputes_for([((0, 4), "abab", "acab")], rng=random.Random(3))
    rendered = adjudicator.render_disputes(disputes)
    assert "abab" in rendered and "acab" in rendered
    assert "audit" not in rendered.lower() and "pipeline" not in rendered.lower()


def test_a_failed_call_is_a_miss_not_a_error(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "boom"

        return Result()

    monkeypatch.setattr(adjudicator.subprocess, "run", fake_run)
    disputes = adjudicator.disputes_for([((0, 4), "abab", "acab")], rng=random.Random(5))
    result = adjudicator.adjudicate(tmp_path / "page.png", disputes)
    assert result.rejected == "exit 1"
    assert adjudicator.confirmed_spans(disputes, result.verdicts) == set()


def test_one_call_carries_every_dispute_on_a_screen(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)

        class Result:
            returncode = 0
            stdout = '{"result": {"verdicts": []}, "total_cost_usd": 0.01, "model": "test"}'
            stderr = ""

        return Result()

    monkeypatch.setattr(adjudicator.subprocess, "run", fake_run)
    places = [((index * 10, index * 10 + 4), f"word{index}", f"ward{index}") for index in range(5)]
    disputes = adjudicator.disputes_for(places, rng=random.Random(13))
    result = adjudicator.adjudicate(tmp_path / "page.png", disputes)
    assert len(calls) == 1, "one screen's disputes must not cost one call each"
    assert result.cost_usd == 0.01
    # Every dispute was skipped by the model, so nothing is confirmed.
    assert adjudicator.confirmed_spans(disputes, result.verdicts) == set()
