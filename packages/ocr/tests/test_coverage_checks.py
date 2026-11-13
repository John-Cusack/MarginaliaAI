"""Gaps are not errors: line coverage and the checks that keep the population honest.

Design §7.3: ink no reader turned into text is a *gap*, which blocks or marks the
page — never an unmarked error. With Kindle's layout line rectangles the test is
exact: every line must yield text, and that text must fit the line's ink width.
These tests pin that test, the extra checks beside it, and the reclassification
that keeps a kern join out of the omission class.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import pipeline_checks as checks  # noqa: E402
from pipeline_errors import agreed_errors  # noqa: E402

LINE = (0.0, 100.0, 600.0, 112.0)


def test_an_empty_line_fails_coverage():
    assert checks.coverage_failures([LINE], [""], median_density=0.2) == (0,)


def test_a_line_far_shorter_than_its_ink_fails_coverage():
    # 600 px wide, 0.2 chars/px => ~120 characters expected; 12 is well short.
    assert checks.coverage_failures([LINE], ["short text"], median_density=0.2) == (0,)
    full = "x" * 110
    assert checks.coverage_failures([LINE], [full], median_density=0.2) == ()


def test_a_full_line_passes_and_density_is_measurable():
    assert checks.character_density("abcd", (0.0, 0.0, 40.0, 10.0)) == 0.1
    assert checks.coverage_failures([LINE], ["a" * 600 * 0 + "x" * 500], median_density=0.5) == ()


def test_a_dash_between_words_is_reported_and_a_hyphen_inside_one_is_not():
    text = "well-known dash \u2014 here and a hyphen-ated word"
    spans = checks.dashed(text)
    assert spans == (text.index("\u2014"), text.index("\u2014") + 1) or spans == (
        (text.index("\u2014"), text.index("\u2014") + 1),
    )
    assert (text.index("-") , text.index("-") + 1) not in spans


def test_numbers_a_page_would_not_print_are_reported():
    assert checks.implausible_numbers("in 1990 the debt") == ()
    assert checks.implausible_numbers("in 199019951997 the debt")


def test_the_running_head_is_reported_where_it_repeats():
    text = "The Rise and Fall of the Great Powers\nchapter text"
    spans = checks.running_head_word(text, head_words=["The Rise and Fall"])
    assert spans and text[spans[0][0] : spans[0][1]].casefold() == "the rise and fall"
    assert checks.running_head_word("chapter text", head_words=["The Rise"]) == ()


def test_a_kern_join_is_a_join_not_an_omission():
    """The measurement's word alignment called 293 of these omissions."""
    # Two truth words, one reading word: "sat" is missing from the word sequence
    # but its letters are inside "catsat", so the ink was read, as one word.
    truth = "the cat sat down"
    readings = {"a": "the catsat down", "b": "the catsat down"}
    errors = agreed_errors(truth, readings, minimum=2)
    kinds = {error.truth_text: error.kind for error in errors}
    assert kinds.get("sat") == "agreed_join"


def test_line_texts_maps_words_to_lines_and_reports_what_it_could_not_place():
    truth = "alpha beta gamma delta"
    runs = [("alpha", 0), ("beta", 0), ("gamma", 1), ("delta", 1)]
    lines, per_line = checks.line_texts(truth, runs, 2)
    assert lines == [0, 0, 1, 1]
    assert per_line == ("alpha beta", "gamma delta")


def test_line_texts_leaves_divergent_words_unplaced():
    truth = "alpha beta gamma"
    runs = [("alpha", 0), ("unrelated", 1)]
    lines, per_line = checks.line_texts(truth, runs, 2)
    assert lines[0] == 0
    assert sum(1 for line in lines if line is None) >= 1
