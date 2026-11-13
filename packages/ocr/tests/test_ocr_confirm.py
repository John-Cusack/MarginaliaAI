"""Confirm, tie-break, mark: what the pipeline will and will not stand behind.

The rule has no 2-of-3, because the errors that matter are the ones readers
share. Everything unconfirmed is marked with its candidates and the reason, and
the tie-break chain is the design's three steps in order - each of them a
decision, never a confirmation.
"""

from __future__ import annotations

import json
from pathlib import Path

from marginalia_ocr.server import checks
from marginalia_ocr.server.confirm import (
    Confirmation,
    Vocabulary,
    check_spans_for,
    confirm_region,
    vocabulary_from_confirmations,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def unanimous(text: str = "the debt was 48.2% and the claim held") -> dict[str, str]:
    return {"tesseract": text, "easyocr": text, "vlm": text}


def test_a_page_every_reader_agrees_on_is_confirmed_with_no_marks():
    result = confirm_region("r0", unanimous())
    assert result.confirmed is True
    assert result.marks == () and result.gaps == ()
    assert result.confirmed_spans == ((0, len(result.text)),)


def test_a_reader_that_produced_nothing_is_not_agreement():
    """An empty or failed reading never counts as agreement (§7.2)."""
    readings = unanimous()
    readings["easyocr"] = ""
    result = confirm_region("r0", readings)
    assert result.confirmed is False
    assert result.marks and any(candidate.text == "" for candidate in result.marks[0].candidates)


def test_a_single_reader_error_is_marked_with_its_candidates():
    """Two readers right and one wrong is still a disagreement, and the mark says
    what the pipeline was choosing between."""
    readings = unanimous()
    readings["easyocr"] = "the debr was 48.2% and the claim held"
    result = confirm_region("r0", readings)

    assert result.confirmed is False
    mark = result.marks[0]
    assert mark.kind == "disagreement"
    texts = {candidate.text for candidate in mark.candidates}
    assert texts == {"the debt was 48.2% and the claim held", "the debr was 48.2% and the claim held"}
    assert {reader for candidate in mark.candidates for reader in candidate.readers} == {
        "tesseract",
        "easyocr",
        "vlm",
    }


def test_a_check_that_says_the_page_is_incomplete_gaps_it_even_unanimously():
    """All readers dropping a line is the case the line-count check exists for."""
    readings = unanimous()
    layout = [(0, i * 14, 100, i * 14 + 12) for i in range(7)]
    per_line = ["one", "two", "three", "four", "five", "six"]      # seven drawn, six read
    result = confirm_region(
        "r0",
        readings,
        checks=(("line_count", checks.line_count_mismatch(layout, per_line)),),
    )
    assert result.confirmed_spans == ()
    assert result.gaps and "not complete" in result.gaps[0].reason


def test_a_check_that_marks_marks_and_does_not_gap():
    readings = unanimous()
    result = confirm_region(
        "r0", readings, checks=(("vocabulary", ((4, 8),)),), gapping=("line_count",)
    )
    assert result.gaps == ()
    assert result.marks[0].kind == "check"
    assert "vocabulary" in result.marks[0].reason


def test_the_reread_tie_break_adopts_one_of_the_readers():
    readings = unanimous()
    readings["easyocr"] = "the debr was 48.2% and the claim held"
    result = confirm_region("r0", readings, reread=lambda: "the debt was 48.2% and the claim held")
    assert result.resolved_by == "reread"
    assert result.text == "the debt was 48.2% and the claim held"
    assert result.marks, "a tie-break is a decision, and it is recorded"


def test_the_book_tie_break_prefers_a_candidate_the_book_confirmed_elsewhere():
    book = Vocabulary.from_texts(["the debt was 48.2% " * 3], minimum=2)
    readings = unanimous()
    readings["easyocr"] = "the debr was 48.2%"
    readings["tesseract"] = "the debt was 48.2%"
    readings["vlm"] = "the debts was 48.2%"
    result = confirm_region("r0", readings, book=book)
    assert result.resolved_by == "book"
    assert result.text == "the debt was 48.2%"


def test_the_vlm_can_only_choose_a_candidate_it_was_given():
    """The chooser cannot propose; an answer that is not a candidate is refused."""
    readings = unanimous()
    readings["easyocr"] = "the debr was 48.2%"

    class Inventive:
        def choose(self, region_id: str, candidates) -> str:
            return "the debt was 48.2% and the claim held, as everyone knows"

    refused = confirm_region("r0", readings, chooser=Inventive())
    assert refused.resolved_by is None
    assert refused.marks[0].kind == "disagreement"
    assert refused.text in {candidate.text for candidate in refused.marks[0].candidates}

    class Honest:
        def choose(self, region_id: str, candidates) -> str:
            return candidates[0]

    adopted = confirm_region("r0", readings, chooser=Honest())
    assert adopted.resolved_by == "vlm"
    assert adopted.text == "the debt was 48.2% and the claim held"


def test_a_tie_break_never_enters_the_book_vocabulary():
    """The vocabulary is what the book confirmed. A tie-break is what it did not."""
    readings = unanimous("the quorum of the committee was recorded")
    readings["easyocr"] = "the quorurn of the committee was recorded"
    tie_break = confirm_region(
        "r0", readings, chooser=type("C", (), {"choose": lambda self, rid, cands: cands[0]})()
    )
    confirmed = confirm_region("r1", unanimous("the quorum met on the first of May"))

    vocabulary = vocabulary_from_confirmations([tie_break, confirmed], minimum=1)

    assert vocabulary.contains("quorum"), "the confirmed page's word is the book's"
    assert vocabulary.contains("committee") is False, "the tie-break's page contributed nothing"
    assert vocabulary.confirmed_texts == (confirmed.text,)


def test_the_vocabulary_only_accepts_confirmed_text_by_construction():
    vocabulary = Vocabulary(minimum=1)
    vocabulary.add_confirmed("the debt was 48.2%")
    assert vocabulary.contains("debt")
    assert not hasattr(vocabulary, "add_candidate"), "there is no way to add a guess"


def test_check_spans_for_asks_every_check_the_design_names():
    vocabulary = checks.book_vocabulary(["the debt was 48.2% " * 3])
    names = [name for name, _spans in check_spans_for("the debr was 48.2%", vocabulary=vocabulary)]
    assert set(names) >= {"vocabulary", "dashed", "digit", "chrome", "split"}


def test_the_recorded_wp0_readings_are_not_all_agreement():
    """Driven from what the three WP0 readers actually produced on a synthetic
    screen: the measurement's own recordings, not a live reader."""
    readings = {
        "tesseract": json.loads((FIXTURES / "readings_tesseract_3x.json").read_text()),
        "easyocr": json.loads((FIXTURES / "readings_easyocr_3x.json").read_text()),
        "vlm": json.loads((FIXTURES / "readings_vlm_3x.json").read_text()),
    }
    shared = set.intersection(*(set(values) for values in readings.values()))
    assert shared, "the recordings should share at least one screen"

    outcomes = [
        confirm_region(screen_id, {reader: values[screen_id] for reader, values in readings.items()})
        for screen_id in sorted(shared)
    ]
    assert any(outcome.marks or outcome.gaps for outcome in outcomes), (
        "three readers are never unanimous on every screen of a real reading"
    )
    for outcome in outcomes:
        assert isinstance(outcome, Confirmation)
        for mark in outcome.marks:
            assert mark.candidates, "a mark without its candidates says nothing"
            assert mark.reason
