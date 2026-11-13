"""Consensus audit: a difference counts only when two readers agree on it.

One reader's noise is not evidence. A local VLM that transcribes and
character-diffs its own transcript reports differences on text that is already
right - a case, a punctuation mark, a re-wrapped line - and singly every one of
those would block the book. Two readers of different families rarely make the
same mistake in the same place, which is what makes their agreement worth
counting, and what makes a disagreement between them worth nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from auditor_consensus import _hit_one, consensus_edits, normalize  # noqa: E402
from ocr_synth.seeded_errors import PlantedError  # noqa: E402


def _error(truth_text: str, final_text: str, start: int, end: int) -> PlantedError:
    """A planted substitution whose span covers ``[start, end)`` of the candidate."""
    return PlantedError(
        kind="real_word",
        final_start=start,
        final_end=end,
        final_text=final_text,
        truth_start=start,
        truth_end=end,
        truth_text=truth_text,
        note="planted",
    )


def test_one_reader_alone_is_not_a_consensus():
    """A difference only one reader reports is dropped, however loud it is."""
    final = "the form of it"
    florence = [(4, 8, "firm")]
    gemma = []
    assert consensus_edits([florence, gemma], final) == []


def test_two_readers_making_different_mistakes_are_not_a_consensus():
    final = "the form of it"
    florence = [(4, 8, "firm")]
    gemma = [(4, 8, "farm")]
    assert consensus_edits([florence, gemma], final) == [], "they disagree, so neither is evidence"


def test_two_readers_agreeing_on_the_truth_are_a_consensus_and_find_the_error():
    final = "the farm of it"          # what the pipeline stored
    florence = [(4, 8, "form")]
    gemma = [(4, 8, " form ")]        # same reading, different whitespace
    agreed = consensus_edits([florence, gemma], final)
    assert [reading for _, _, reading in agreed] == ["form"]
    error = _error("form", "farm", 4, 8)
    assert _hit_one(error, agreed) is True


def test_the_majority_of_three_carries_an_agreement_too():
    final = "the farm of it"
    agree = [(4, 8, "form")]
    other = [(4, 8, "firm")]
    assert consensus_edits([agree, agree, other], final, minimum=2) == [(4, 8, "form")]


def test_an_auditor_that_reports_the_whole_page_finds_nothing():
    """The pathological case, through the consensus path as well as the strict one."""
    final = "the farm of it"
    noisy = [(0, len(final), final)]     # reports the stored text back, everywhere
    assert consensus_edits([noisy, noisy], final) == []


def test_agreement_on_a_reading_that_is_not_the_truth_is_not_a_finding():
    final = "the farm of it"
    both_same_mistake = [(4, 8, "firm")]
    agreed = consensus_edits([both_same_mistake, both_same_mistake], final)
    assert len(agreed) == 1, "they do agree on a difference"
    error = _error("form", "farm", 4, 8)
    assert _hit_one(error, agreed) is False, "but not on the truth, so the error is not found"


def test_the_normalization_is_the_aligners_not_a_reader_s():
    """NFC, ligatures expanded, whitespace collapsed; case and punctuation stay."""
    assert normalize("  the   form ") == "the form"
    assert normalize("the \ufb01ne point") == "the fine point"
    assert normalize("Form") != normalize("form"), "case is significant in 7.4"
    assert normalize("form.") != normalize("form"), "so is punctuation"
