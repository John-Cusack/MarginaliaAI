"""Alignment: the one place the comparison rule lives (design 7.4).

WP0 measured with this code; WP2 reads with it. The tests are the rule itself:
what is folded (NFC, ligatures, whitespace) and what is emphatically not (case,
punctuation, signs, decimals, accents).
"""

from __future__ import annotations

from marginalia_ocr.server.align import Edit, classify_errors, normalize, overlaps


def kinds(truth: str, final: str) -> list[str]:
    return [edit.kind for edit in classify_errors(truth, final)]


def test_the_normalization_folds_exactly_what_the_design_says():
    assert normalize("  the   form ") == "the form"
    assert normalize("caf\u00e9") == "caf\u00e9", "NFC, not NFD"
    assert normalize("the \ufb01ne point") == "the fine point", "ligatures are expanded"


def test_a_case_difference_is_a_difference():
    """§7.4: case is significant. The WP0 measurement folded nothing, and neither
    does this: a lowercase 'us debt' is not the page's 'US debt'."""
    assert normalize("US") != normalize("us")
    assert classify_errors("US debt", "us debt") != ()


def test_us_minus_one_point_five_percent_is_not_us_one_point_five():
    """The plan's example, at the character level it is meant at.

    '-1.5%' and '1.5' differ by a sign, a decimal point and a percent sign. None
    of those is folded, so they are three differences and never agreement - a
    percent sign lost in a table is exactly the kind of error this catches.
    """
    truth = "US debt: -1.5%"
    final = "us debt: 1.5"
    edits = classify_errors(truth, final)
    assert edits, "a sign, a decimal point, a percent sign and a case are differences"
    assert normalize(truth) != normalize(final)
    assert {edit.kind for edit in edits} & {"substitution", "omission", "insertion", "punctuation"}


def test_an_edit_is_reported_on_both_sides():
    """Character level, and in both coordinate spaces: an edit is a fact about a
    pair of texts, so it has a span in each of them."""
    edits = classify_errors("the form of it", "the farm of it")
    assert len(edits) == 1
    edit: Edit = edits[0]
    assert edit.kind == "substitution"
    assert (edit.truth_text, edit.final_text) == ("o", "a")
    assert (edit.truth_start, edit.truth_end) == (5, 6)
    assert (edit.final_start, edit.final_end) == (5, 6), "the same place in the other text"


def test_an_omission_is_an_edit_and_a_word_only_one_reading_has_is_a_disagreement():
    assert kinds("the quick brown fox", "the brown fox") == ["omission"]
    assert kinds("the quick brown fox", "the quick brown fox jumps") == ["insertion"]


def test_a_transposition_is_one_order_edit_not_four_substitutions():
    assert kinds("the form of it", "the of form it") == ["order"]


def test_identical_text_has_no_edits():
    assert classify_errors("a page of text", "a page of text") == ()


def test_overlaps_treats_an_empty_span_as_a_point_with_a_window():
    assert overlaps([(10, 20)], (20, 20)) is True, "a point at the edge of the edit"
    assert overlaps([(10, 20)], (5, 5)) is False
    assert overlaps([(10, 20)], (15, 25)) is True
