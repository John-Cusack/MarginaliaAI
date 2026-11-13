"""Confirm per word, not per region.

A 300-word region with one disputed word is one mark of one word: everything else
is confirmed. That is the contract, and it is what keeps a page from being almost
entirely uncertain the moment one reader slips on one word.

The stored text keeps the primary reading's own whitespace and line breaks -
normalization is for comparing, never for storing - and where a disputed segment
has no tie-break the stored word is the primary's, with the mark saying so.
"""

from __future__ import annotations

import json
from pathlib import Path

from marginalia_ocr.server import checks
from marginalia_ocr.server.confirm import (
    Candidate,
    Confirmation,
    Vocabulary,
    check_spans_for,
    confirm_readings,
    vocabulary_from_confirmations,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def three(text: str) -> dict[str, str]:
    """The same text from all three readers, with the VLM as the primary."""
    return {"tesseract": text, "easyocr": text, "vlm": text}


def words(count: int) -> str:
    return " ".join(f"w{index}" for index in range(count))


class Chooser:
    def __init__(self, answer: int = 0) -> None:
        self.answer = answer
        self.seen: list[tuple[str, tuple[str, ...]]] = []

    def choose(self, segment_id: str, candidates) -> str | None:
        self.seen.append((segment_id, tuple(candidates)))
        return candidates[self.answer] if candidates else None


def test_a_page_every_reader_agrees_on_is_confirmed_with_no_marks():
    result = confirm_readings(three("the debt was 48.2% and the claim held"))
    assert result.confirmed is True
    assert result.marks == () and result.gaps == ()
    assert result.confirmed_spans == ((0, len(result.text)),)


def test_one_disputed_word_marks_that_word_and_confirms_the_rest():
    """The defect this replaced: the whole region was marked uncertain."""
    text = words(200)
    readings = three(text)
    readings["easyocr"] = text.replace("w99", "W99")

    result = confirm_readings(readings)

    assert len(result.marks) == 1, [mark.span for mark in result.marks]
    mark = result.marks[0]
    assert result.text[mark.span[0] : mark.span[1]].strip() == "w99"
    confirmed_words = sum(len(result.text[start:end].split()) for start, end in result.confirmed_spans)
    assert confirmed_words == 199, confirmed_words
    assert set(mark.candidates) == {
        Candidate(text="w99", readers=("tesseract",)),
        Candidate(text="W99", readers=("easyocr",)),
        Candidate(text="w99", readers=("vlm",)),
    } or {candidate.text for candidate in mark.candidates} == {"w99", "W99"}


def test_a_check_hit_marks_only_its_own_span():
    text = "the debt was 48.2% and the claim held"
    result = confirm_readings(three(text), checks=(("vocabulary", ((4, 8),)),))

    assert result.gaps == ()
    assert len(result.marks) == 1
    start, end = result.marks[0].span
    assert result.text[start:end] == "debt"
    confirmed = [result.text[a:b] for a, b in result.confirmed_spans]
    assert any("48.2%" in piece for piece in confirmed), "the rest of the page stands"


def test_a_missing_line_gaps_the_page_even_when_the_readers_agree():
    text = words(40)
    layout = [(0, i * 14, 100, i * 14 + 12) for i in range(41)]
    per_line = text.split() * 1
    result = confirm_readings(
        three(text), checks=(("line_count", checks.line_count_mismatch(layout, per_line)),)
    )
    assert result.confirmed_spans == ()
    assert result.gaps and "not complete" in result.gaps[0].reason


def test_the_stored_text_keeps_the_primarys_line_breaks():
    text = "The debt was 48.2%\nand the claim held.\n\nA new paragraph began."
    readings = three(text)
    readings["easyocr"] = text.replace("48.2%", "482%")

    result = confirm_readings(readings)

    assert "\n" in result.text and "\n\n" in result.text, "the layout was collapsed"
    assert result.text.startswith("The debt was")


def test_a_dispute_with_no_tie_break_keeps_the_primary_and_says_so():
    readings = three("the debt was 48.2%")
    readings["easyocr"] = "the debr was 48.2%"
    result = confirm_readings(readings)

    assert result.text == "the debt was 48.2%", "the primary (vlm) reading is stored"
    mark = result.marks[0]
    assert "vlm" in mark.reason and "not confirmed" in mark.reason
    # the mark is the one disputed word, not the whole sentence
    assert result.text[mark.span[0] : mark.span[1]] == "debt"
    assert {candidate.text for candidate in mark.candidates} == {"debt", "debr"}


def test_the_tie_break_is_per_segment():
    """Two disputes on one page are two decisions, not one."""
    readings = three("the debt was 48.2% and the claim held")
    readings["easyocr"] = "the debr was 48.2% and the claime held"
    chooser = Chooser(answer=0)

    result = confirm_readings(readings, chooser=chooser)

    assert len(chooser.seen) == 2, chooser.seen
    assert len(result.marks) == 2
    assert all(mark.kind == "tiebreak:vlm" for mark in result.marks)
    assert result.text == "the debt was 48.2% and the claim held"


def test_the_chooser_can_only_pick_a_candidate():
    readings = three("the debt was 48.2%")
    readings["easyocr"] = "the debr was 48.2%"

    class Inventive:
        def choose(self, segment_id: str, candidates) -> str:
            return "the debt was 48.2% and the reader is quite sure"

    refused = confirm_readings(readings, chooser=Inventive())
    assert refused.marks[0].kind == "disagreement"
    assert refused.text == "the debt was 48.2%"


def test_the_book_tie_break_needs_exactly_one_qualifying_candidate():
    book = Vocabulary.from_texts(["the debt was 48.2% " * 3], minimum=2)
    readings = three("the debt was 48.2%")
    readings["easyocr"] = "the debt was 482%"
    readings["tesseract"] = "the debts was 48.2%"

    result = confirm_readings(readings, book=book)

    assert result.text == "the debt was 48.2%"
    assert result.marks[0].kind == "tiebreak:book"


def test_a_tie_break_never_enters_the_book_vocabulary():
    # The disputed word is one no confirmed span ever carries: if a tie-break
    # could seed the vocabulary, this is the word that would appear in it.
    readings = three("the sequence ended at quorum")
    readings["easyocr"] = "the sequence ended at quorurn"
    readings["vlm"] = "the sequence ended at quorurn"
    tie_break = confirm_readings(readings, chooser=Chooser(answer=0))
    assert tie_break.marks, "the page should have had a dispute to resolve"
    confirmed = confirm_readings(three("the sequence continued on the first of May"))

    vocabulary = vocabulary_from_confirmations([tie_break, confirmed], minimum=1)

    assert vocabulary.contains("sequence"), "the confirmed pages' words are the book's"
    assert vocabulary.contains("quorum") is False, (
        "a tie-break decided it, so the book has not confirmed it"
    )
    assert vocabulary.contains("quorurn") is False
    # The vocabulary records the *spans* that were confirmed, so a page which had
    # a dispute still contributes its confirmed words - and not the disputed one.
    assert "sequence" in " ".join(vocabulary.confirmed_texts)
    assert "quorum" not in " ".join(vocabulary.confirmed_texts)


def test_the_vocabulary_is_exact_and_the_docstring_says_so():
    """§7.4 makes case significant; the vocabulary must not fold it."""
    vocabulary = Vocabulary.from_texts(["the Margin of error " * 3], minimum=2)
    assert vocabulary.contains("Margin")
    assert vocabulary.contains("margin") is False


def test_check_spans_for_asks_every_check_the_design_names():
    vocabulary = checks.book_vocabulary(["the debt was 48.2% " * 3])
    names = [name for name, _spans in check_spans_for("the debr was 48.2%", vocabulary=vocabulary)]
    assert set(names) >= {"vocabulary", "dashed", "digit", "chrome", "split"}


def test_the_recorded_wp0_readings_drive_the_same_code():
    """The measurement's own recordings of three readers on synthetic screens."""
    readings = {
        "tesseract": json.loads((FIXTURES / "readings_tesseract_3x.json").read_text()),
        "easyocr": json.loads((FIXTURES / "readings_easyocr_3x.json").read_text()),
        "vlm": json.loads((FIXTURES / "readings_vlm_3x.json").read_text()),
    }
    shared = set.intersection(*(set(values) for values in readings.values()))
    assert shared

    outcomes = [
        confirm_readings({reader: values[screen_id] for reader, values in readings.items()})
        for screen_id in sorted(shared)
    ]
    assert any(outcome.marks or outcome.gaps for outcome in outcomes)
    for outcome in outcomes:
        assert isinstance(outcome, Confirmation)
        for mark in outcome.marks:
            assert mark.candidates and mark.reason


def test_a_blank_primary_does_not_swallow_the_other_readers():
    """The bug this test exists for: {'vlm': '', 'tess': 'alpha beta gamma', ...}
    returned stored text '' with no marks and no gaps - two readers' text gone."""
    readings = {"vlm": "", "tesseract": "alpha beta gamma", "third": "alpha beta gamma"}
    result = confirm_readings(readings, region_has_ink=True)

    assert result.text == "alpha beta gamma", "the readers who did read were thrown away"
    assert result.marks, "and the region was not even marked"
    mark = result.marks[0]
    assert "primary reader produced nothing" in mark.reason
    assert {candidate.text for candidate in mark.candidates} == {"", "alpha beta gamma"}
    assert result.confirmed_spans == (), "nothing about a region with a failed primary is confirmed"


def test_every_reader_blank_on_an_inked_region_gaps_the_page():
    """A blank reading is not a blank page: the inventory says there is ink."""
    result = confirm_readings(
        {"vlm": "", "tesseract": "", "third": ""}, region_has_ink=True
    )
    assert result.gaps and "inventory says has ink" in result.gaps[0].reason
    assert result.confirmed_spans == ()


def test_every_reader_blank_on_a_region_with_no_ink_is_just_empty():
    result = confirm_readings({"vlm": "", "tesseract": "", "third": ""}, region_has_ink=False)
    assert result.gaps == () and result.marks == ()


def test_nothing_a_reader_saw_ever_disappears_unrecorded():
    """The property, over random small readings with blanks, insertions and
    omissions at the start and the end: every word any reader produced is either
    in the stored text or in some mark's candidates."""
    import random

    rng = random.Random(20261113)
    words = ["alpha", "beta", "gamma", "delta"]

    def random_reading() -> str:
        kept = [word for word in words if rng.random() > 0.25]
        if rng.random() < 0.15:
            kept = []
        if kept and rng.random() < 0.2:
            kept = ["extra"] + kept
        if kept and rng.random() < 0.2:
            kept = kept + ["tail"]
        return " ".join(kept)

    for _ in range(300):
        readings = {reader: random_reading() for reader in ("vlm", "tesseract", "third")}
        result = confirm_readings(readings, region_has_ink=any(readings.values()))

        produced = {word for text in readings.values() for word in text.split()}
        if not produced:
            assert result.gaps or result.text == ""
            continue

        stored = set(result.text.split())
        in_candidates = {
            word
            for mark in result.marks
            for candidate in mark.candidates
            for word in candidate.text.split()
        }
        missing = produced - stored - in_candidates
        assert not missing, f"{missing} vanished; readings={readings}, stored={result.text!r}"
