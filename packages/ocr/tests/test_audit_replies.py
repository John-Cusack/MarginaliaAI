"""Auditor replies: a rejected answer is never "no differences", and long places
have a location-only form.

Two rules from the operator, both about not letting a validation failure look like
agreement: a rejected reply is re-asked once and then counts as a detection at the
gate and a miss in the measurement; and a place that differs too much for eight
words is reported as a line range with no reading at all, so the diff-only cap
holds while the difference is still found.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import claude_diff_checker as checker  # noqa: E402


def test_a_location_only_difference_is_accepted_and_carries_no_text():
    payload = {"differences": [{"lines": [10, 40], "kind": "differs_extensively"}]}
    (difference,) = checker.validate(payload, candidate_length=100)
    assert difference.snippet == "" and difference.reading == ""
    assert (difference.final_start, difference.final_end) == (10, 40)
    assert difference.located


@pytest.mark.parametrize(
    "lines",
    [[10], [40, 10], [-1, 10], [10, 200], ["a", "b"]],
)
def test_a_bad_location_is_rejected(lines):
    payload = {"differences": [{"lines": lines, "kind": "differs_extensively"}]}
    with pytest.raises(checker.ResponseRejected):
        checker.validate(payload, candidate_length=100)


def test_an_over_long_snippet_is_structural_and_rejected():
    """The snippet *is* the location; without a usable one there is nothing to place."""
    payload = {"differences": [{"snippet": " ".join(["w"] * 9), "reading": "x"}]}
    with pytest.raises(checker.ResponseRejected):
        checker.validate(payload, candidate_length=100)


def test_an_over_long_reading_is_downgraded_not_rejected():
    """The operator's rule: discard the text, keep the location, keep the reply.

    No over-long text is stored, logged or scored, so the cap is not loosened —
    the difference simply arrives as a location instead of a reading.
    """
    payload = {
        "differences": [
            {"snippet": "the debt was", "reading": " ".join(["w"] * 12)},
            {"snippet": "short one", "reading": "brief one"},
        ]
    }
    first, second = checker.validate(payload, candidate_length=100)
    assert first.downgraded is True
    assert first.reading == "", "the over-long reading is discarded"
    assert first.snippet == "the debt was", "its location is kept"
    assert second.downgraded is False and second.reading == "brief one"


def _stub(monkeypatch, replies: list[str]):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)

        class Result:
            returncode = 0
            stderr = ""

            def __init__(self):
                self.stdout = replies[min(len(calls) - 1, len(replies) - 1)]

        return Result()

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    return calls


def test_a_rejected_reply_is_reasked_once_then_reported(monkeypatch, tmp_path):
    bad = json.dumps(
        {"structured_output": {"differences": [{"snippet": " ".join(["w"] * 9), "reading": "x"}]}}
    )
    calls = _stub(monkeypatch, [bad, bad])
    result = checker.check(tmp_path / "p.png", "candidate text")
    assert len(calls) == 2, "a rejected reply must be re-asked once"
    assert result.rejected is not None
    assert result.attempts == 2
    assert result.differences == ()


def test_a_rejected_reply_that_succeeds_on_the_second_ask_is_used(monkeypatch, tmp_path):
    bad = json.dumps(
        {"structured_output": {"differences": [{"snippet": " ".join(["w"] * 9), "reading": "x"}]}}
    )
    good = json.dumps(
        {"structured_output": {"differences": [{"lines": [0, 4], "kind": "differs_extensively"}]}}
    )
    calls = _stub(monkeypatch, [bad, good])
    result = checker.check(tmp_path / "p.png", "candidate text")
    assert len(calls) == 2
    assert result.rejected is None
    assert result.attempts == 2
    assert (result.differences[0].final_start, result.differences[0].final_end) == (0, 4)
