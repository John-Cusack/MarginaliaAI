"""The second seeded set: the mistakes the readers agreed on.

The planted set measures an auditor against errors a seeder invented. The errors
that actually escape a three-reader confirm step are the ones every reader made,
so this set is built from the readers' own text — and the property that makes it
usable is the same one the planted sets have: undoing every recorded span must
give the page back exactly.
"""

from __future__ import annotations

from ocr_synth.seeded_errors import revert
from pipeline_errors import agreed_errors, build_pipeline_set
from reader_guards import guard_reading

TRUTH = "The debt was 48.2% and the form of the claim changed."


def test_a_word_two_readers_got_wrong_the_same_way_is_an_agreed_error():
    readings = {
        "tesseract": "The debt was 48.2% and the from of the claim changed.",
        "easyocr": "The debt was 48.2% and the from of the claim changed.",
        "vlm": TRUTH,
    }
    errors = agreed_errors(TRUTH, readings, minimum=2)
    assert [(error.truth_text, error.agreed_text, error.kind) for error in errors] == [
        ("form", "from", "agreed_substitution")
    ]
    assert errors[0].readers == ("easyocr", "tesseract")
    assert agreed_errors(TRUTH, readings, minimum=3) == ()


def test_a_disagreement_is_not_an_agreed_error():
    readings = {
        "tesseract": "The debt was 48.8% and the form of the claim changed.",
        "easyocr": "The debt was 48.9% and the form of the claim changed.",
        "vlm": TRUTH,
    }
    assert agreed_errors(TRUTH, readings, minimum=2) == ()


def test_readers_that_drop_the_same_word_agree_on_an_omission():
    """Word granularity is what the aligner sees: dropping the last word is one
    deletion, dropping a phrase is one per word, and both are honest counts of
    what the readers dropped."""
    readings = {
        "tesseract": "The debt was 48.2% and the form of the claim",
        "easyocr": "The debt was 48.2% and the form of the claim",
        "vlm": TRUTH,
    }
    errors = agreed_errors(TRUTH, readings, minimum=2)
    assert [error.kind for error in errors] == ["agreed_omission"]
    assert errors[0].agreed_text == "" and errors[0].truth_text == "changed."


def test_dropping_a_phrase_is_reported_word_by_word():
    readings = {
        "tesseract": "The debt was 48.2% and the claim changed.",
        "easyocr": "The debt was 48.2% and the claim changed.",
    }
    dropped = {error.truth_text for error in agreed_errors(TRUTH, readings, minimum=2)}
    assert {"form", "of"} <= dropped


def test_the_pipeline_candidate_carries_the_agreed_errors_and_reverts():
    readings = {
        "tesseract": "The debt was 48.2% and the from of the claim changed.",
        "easyocr": "The debt was 48.2% and the from of the claim changed.",
        "vlm": "The debt was 48.2% and the from of the claim changed.",
    }
    seeded, errors = build_pipeline_set(TRUTH, readings, minimum=3)
    assert "from of the claim" in seeded.final
    assert "form of the claim" not in seeded.final
    assert len(seeded.errors) == len(errors) == 1
    assert revert(seeded) == TRUTH, "the pipeline set must revert like the planted one"


def test_a_clean_agreement_produces_an_empty_set():
    seeded, errors = build_pipeline_set(TRUTH, {"tesseract": TRUTH, "easyocr": TRUTH}, minimum=2)
    assert errors == () and seeded.final == TRUTH and seeded.errors == ()


def test_the_agreed_errors_survive_the_repetition_guard():
    """A pipeline set is built from a reader's text, so it can contain a loop;
    the guard is what keeps that out of the measurement."""
    loop = "a list of revisions read. " * 40
    assert guard_reading(loop).gap is True
    readings = {"tesseract": loop, "easyocr": loop}
    seeded, _ = build_pipeline_set(TRUTH, readings, minimum=2)
    assert seeded.final != TRUTH
