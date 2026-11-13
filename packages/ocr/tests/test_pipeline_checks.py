"""The independent checks, and the population they leave the auditor to be judged on.

A span the checks would mark is marked in production, so it is not an error the
audit has to catch; a case-only difference is its own class and its own operator
decision. Both are kept out of `s_L`, and both are counted where they happen.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import pipeline_checks as checks  # noqa: E402
from pipeline_errors import agreed_errors, build_pipeline_set  # noqa: E402

BOOK = [
    "The debt was large and the debt was old.",
    "He repaid the debt in the spring, and the debt was gone.",
    "Margin notes beside the debt.",
]


def test_vocabulary_is_the_books_own_words():
    vocabulary = checks.book_vocabulary(BOOK, minimum=3)
    assert "debt" in vocabulary and "the" in vocabulary
    assert "margin" not in vocabulary, "a word used once is not vocabulary"


def test_a_confusion_is_caught_and_a_real_word_is_not():
    vocabulary = checks.book_vocabulary(BOOK, minimum=3)
    text = "He repaid the debr in the spring."
    flagged = checks.unmatched_words(text, vocabulary)
    assert (text.index("debr"), text.index("debr") + 4) in flagged

    # A word the book uses is not flagged, whichever weak words a three-sentence
    # book also fails to establish.
    clean = "He repaid the debt in the spring."
    debt = (clean.index("debt"), clean.index("debt") + 4)
    assert not checks.catches(checks.unmatched_words(clean, vocabulary), debt)


def test_a_case_change_is_caught_by_the_vocabulary_check():
    """Which is why case errors leave the population twice over: class and check."""
    vocabulary = checks.book_vocabulary(BOOK, minimum=3)
    assert checks.unmatched_words("He repaid the DEBT in the spring.", vocabulary)


def test_catches_covers_the_word_a_planted_error_sits_in():
    assert checks.catches(((5, 9),), (6, 8)) is True
    assert checks.catches(((5, 9),), (10, 12)) is False


def test_a_case_only_error_is_its_own_class():
    truth = "The debt was large and the debt was old."
    readings = {"a": "The DEBT was large and the debt was old.", "b": "The DEBT was large and the debt was old."}
    errors = agreed_errors(truth, readings, minimum=2)
    assert [error.kind for error in errors] == ["agreed_case"]


def test_skip_keeps_the_skipped_error_out_and_the_candidate_consistent():
    truth = "The debt was large and the debt was old."
    readings = {"a": "The DEBT was large and the debr was old.", "b": "The DEBT was large and the debr was old."}
    page, errors = build_pipeline_set(truth, readings, minimum=2)
    assert {error.kind for error in errors} == {"agreed_case", "agreed_substitution"}

    kept, kept_errors = build_pipeline_set(
        truth, readings, minimum=2, skip=lambda error: error.kind == "agreed_case"
    )
    assert [error.kind for error in kept_errors] == ["agreed_substitution"]
    assert "DEBT" not in kept.final, "a skipped error is left as the truth: the marked span was repaired"
    assert "debt" in kept.final
    assert "debr" in kept.final
    from ocr_synth.seeded_errors import revert

    assert revert(kept) == kept.truth, "the seeded set still reverts through its own recorded spans"
