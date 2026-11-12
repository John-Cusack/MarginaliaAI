"""Planted errors: reproducible, reversible, and honest about their limits."""

from __future__ import annotations

import random

import pytest
from ocr_synth import text as T
from ocr_synth.corpus import build_corpus
from ocr_synth.seeded_errors import (
    SEEDED_COUNTS,
    NoMaterial,
    build_seeded,
    can_carry,
    plant,
    revert,
    with_counts,
)

PAGE = "\n\n".join((T.PROSE[1], T.PROSE[2], T.PROSE[3]))


def test_planting_is_reproducible():
    first = plant(PAGE, random.Random("seed"))
    second = plant(PAGE, random.Random("seed"))
    assert first == second
    assert first.final != plant(PAGE, random.Random("other")).final


def test_reverting_every_planted_error_reproduces_the_truth():
    seeded = plant(PAGE, random.Random("seed"))
    assert seeded.planted == sum(SEEDED_COUNTS.values())
    assert seeded.final != seeded.truth
    assert revert(seeded) == seeded.truth


def test_every_class_is_planted_the_predeclared_number_of_times():
    counts: dict[str, int] = {}
    for error in plant(PAGE, random.Random("seed")).errors:
        counts[error.kind] = counts.get(error.kind, 0) + 1
    assert counts == SEEDED_COUNTS


def test_recorded_spans_address_the_recorded_text():
    seeded = plant(PAGE, random.Random("seed"))
    for error in seeded.errors:
        assert seeded.final[error.final_start : error.final_end] == error.final_text
        assert seeded.truth[error.truth_start : error.truth_end] == error.truth_text


def test_real_word_plantings_are_real_words_of_the_same_length():
    for error in plant(PAGE, random.Random("seed")).errors:
        if error.kind == "real_word":
            assert error.final_text.isalpha() and error.truth_text.isalpha()
            assert len(error.final_text) == len(error.truth_text)


def test_zero_counts_change_nothing():
    seeded = plant(PAGE, random.Random("seed"), counts=dict.fromkeys(SEEDED_COUNTS, 0))
    assert seeded.final == seeded.truth
    assert seeded.errors == ()


def test_a_page_without_material_is_refused_not_quietly_trimmed():
    with pytest.raises(NoMaterial):
        plant("", random.Random("seed"))
    with pytest.raises(NoMaterial):
        plant("short text here", random.Random("seed"), counts=with_counts({"digit": 3}))
    assert can_carry(PAGE)
    assert not can_carry("short text here", counts=with_counts({"digit": 3}))


def test_every_prose_page_of_the_corpus_carries_the_predeclared_set():
    screens = [screen for screen in build_corpus(dprs=(1,), kinds=("two_column_prose",))]
    seeded = build_seeded(screens)
    assert len(seeded) == len(screens)
    for page in seeded:
        assert revert(page) == page.truth
        assert page.planted == sum(SEEDED_COUNTS.values())


def test_a_sparse_page_cannot_carry_the_real_word_class():
    sparse = next(
        screen for screen in build_corpus(dprs=(1,), kinds=("sparse_page",))
    )
    assert not can_carry(sparse.truth_text), "the sensitivity set must be drawn from pages that can carry it"


def test_a_page_with_runs_of_two_spaces_reverts():
    """A Markdown table's empty cell is three spaces wide; rebuilding the page
    from space-split tokens used to collapse them, and the reversion check
    caught it. This is that regression."""
    truth = "| Year | Debt | Notes |\n| --- | --- | --- |\n| 2005 | 44.3 |   |"
    assert "   |" in truth
    for seed in ("a", "b", "c"):
        seeded = plant(
            truth,
            random.Random(seed),
            counts=with_counts({"real_word": 0, "digit": 1, "omission": 1, "insertion": 1, "punctuation": 0, "order": 1}),
        )
        assert revert(seeded) == truth


def test_every_screen_that_carries_the_synthetic_set_reverts_it():
    from ocr_synth.corpus import build_corpus

    counts = {"real_word": 1, "digit": 1, "omission": 1, "insertion": 1, "punctuation": 1, "order": 1}
    for screen in build_corpus(dprs=(1,)):
        if not can_carry(screen.truth_text, counts):
            continue
        seeded = plant(screen.truth_text, random.Random("revert"), counts)
        assert revert(seeded) == screen.truth_text, screen.screen_id


def test_build_seeded_is_identical_across_runs():
    screens = build_corpus(dprs=(1,), kinds=("two_column_prose",))
    assert build_seeded(screens) == build_seeded(screens)
