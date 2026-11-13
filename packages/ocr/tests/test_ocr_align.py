"""Alignment: the one place the comparison rule lives (design 7.4).

WP0 measured with this code; WP2 reads with it. The tests are the rule itself:
what is folded (NFC, ligatures, whitespace) and what is emphatically not (case,
punctuation, signs, decimals, accents).
"""

from __future__ import annotations

from marginalia_ocr.server.align import (
    AGREED,
    DISPUTED,
    Edit,
    align_readings,
    classify_errors,
    normalize,
    overlaps,
)


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


def test_alignment_is_per_word_and_resynchronizes():
    """One disputed word is one disputed word, not the rest of the page.

    A 200-word region where the readers disagree about one word must come back as
    three segments - agreed, disputed, agreed - with the dispute exactly one word
    wide. Without resynchronization one bad word drags every word after it.
    """
    words = [f"w{i}" for i in range(200)]
    truth = " ".join(words)
    readings = {
        "tesseract": truth,
        "easyocr": " ".join(["w0"] + ["W99"] + [f"w{i}" for i in range(1, 200)]),
        "vlm": truth,
    }
    readings["easyocr"] = truth.replace("w99", "W99")

    segments = align_readings(readings, primary="vlm")

    disputed = [segment for segment in segments if segment.kind == DISPUTED]
    assert len(disputed) == 1, f"{len(disputed)} disputes for one wrong word"
    assert disputed[0].text.strip() == "w99", disputed[0].text
    assert sum(len(segment.text.split()) for segment in segments if segment.kind == AGREED) == 199
    assert [segment.kind for segment in segments] == [AGREED, DISPUTED, AGREED]


def test_a_word_one_reader_saw_and_the_others_did_not_is_a_visible_dispute():
    """An insertion is a dispute with a candidate, not an invisible difference."""
    segments = align_readings(
        {"tesseract": "the debt was 48.2%", "easyocr": "the debt was 48.2%", "vlm": "the debt was"},
        primary="vlm",
    )
    disputed = [segment for segment in segments if segment.kind == DISPUTED]
    assert disputed, "the extra token vanished"
    candidate = dict(disputed[0].candidates)["easyocr"]
    assert "48.2%" in candidate, candidate
    assert disputed[0].text.strip() == "was"


def test_an_empty_reading_is_a_candidate_not_silence():
    """A reader that produced nothing is a disagreement, and the empty answer is
    recorded as its candidate rather than treated as absence of an opinion."""
    segments = align_readings(
        {"tesseract": "the debt was 48.2%", "easyocr": "", "vlm": "the debt was 48.2%"},
        primary="vlm",
    )
    assert len(segments) == 1 and segments[0].kind == DISPUTED
    assert dict(segments[0].candidates)["easyocr"] == ""
    assert dict(segments[0].candidates)["vlm"] == "the debt was 48.2%"


def test_alignment_keeps_the_primarys_line_breaks():
    """Normalization is for comparing; storage keeps the layout of the book."""
    text = "The debt was 48.2%\nand the claim held.\n\nA new paragraph began."
    segments = align_readings(
        {"tesseract": text, "easyocr": text, "vlm": text}, primary="vlm"
    )
    assert len(segments) == 1
    assert "\n" in segments[0].text and "\n\n" in segments[0].text
