"""Scoring, marking, and the two bounds the release rule rests on.

The numbers in this file are the audit's arithmetic. A mistake here would not
crash anything: it would let a book be released, which is why the bound is
tested against its defining property rather than a remembered constant.
"""

from __future__ import annotations

import pytest
from ocr_synth import text as T
from ocr_synth.corpus import build_corpus
from ocr_synth.metrics import (
    Detection,
    aggregate,
    audit_sensitivity,
    classify_errors,
    clopper_pearson_lower,
    clopper_pearson_upper,
    normalize,
    release_allowed,
    release_bound,
    rule_of_three,
    score_page,
    tail_probability,
)
from ocr_synth.seeded_errors import PlantedError, SeededText, build_seeded

PAGE = "\n\n".join((T.PROSE[1], T.PROSE[2]))


def kinds(truth: str, final: str) -> list[str]:
    return [edit.kind for edit in classify_errors(truth, final)]


def test_identical_text_has_no_edits():
    assert classify_errors(PAGE, PAGE) == ()


def test_a_character_substitution_is_a_substitution():
    edits = classify_errors("the form of it", "the farm of it")
    assert [edit.kind for edit in edits] == ["substitution"]
    assert edits[0].truth_text == "o" and edits[0].final_text == "a"


def test_a_two_character_swap_inside_a_word_is_two_substitutions():
    """Not an insertion plus an omission: a reader that writes `from` for
    `form` changed two characters in place, and the audit must see it that way."""
    assert kinds("the form of it", "the from of it") == ["substitution", "substitution"]


def test_a_changed_digit_is_a_digit_edit():
    assert kinds("debt was 48.2%", "debt was 48.8%") == ["digit"]


def test_changed_punctuation_is_a_punctuation_edit():
    assert kinds("a dollar price, in a till", "a dollar price; in a till") == ["punctuation"]


def test_a_dropped_word_is_an_omission():
    assert kinds("the margin is not empty space", "the margin is not space") == ["omission"]


def test_an_added_word_is_an_insertion():
    assert kinds("the margin is empty", "the margin is not empty") == ["insertion"]


def test_a_swapped_pair_is_one_order_edit():
    edits = classify_errors("read the margin as evidence", "read the margin evidence as")
    assert [edit.kind for edit in edits] == ["order"]
    assert edits[0].final_text == "evidence as"
    assert edits[0].truth_text == "as evidence"


def test_marked_errors_are_not_unmarked_errors():
    truth = "the debt was 48.2% in 1990"
    final = "the debt was 48.8% in 1990"
    edit = classify_errors(truth, final)[0]
    unmarked = score_page(truth, final)
    marked = score_page(truth, final, marks=[edit.final_span])
    assert unmarked.unmarked_errors == 1
    assert unmarked.marked_errors == 0
    assert marked.unmarked_errors == 0
    assert marked.marked_errors == 1
    assert marked.marks_on_correct_text == 0


def test_a_mark_on_correct_text_is_counted_but_costs_no_error():
    truth = "the debt was 48.2%"
    score = score_page(truth, truth, marks=[(0, 3)])
    assert score.errors == 0
    assert score.marks_on_correct_text == 1


def test_gaps_are_reported_verbatim():
    score = score_page(PAGE, PAGE, gaps=[(10, 20)])
    assert score.gaps == ((10, 20),)
    assert score.by_kind == {}


def test_an_omission_is_marked_by_a_nearby_mark():
    truth = "the margin is not empty space"
    final = "the margin is not space"
    score = score_page(truth, final, marks=[(15, 19)])
    assert score.errors == 1
    assert score.marked_errors == 1


def test_aggregate_counts_pages_marks_and_errors_per_bad_page():
    clean = score_page(PAGE, PAGE)
    bad = score_page("debt was 48.2%", "debt was 48.8%")
    marked = score_page("debt was 48.2%", "debt was 48.8%", marks=[(9, 13)])
    table = aggregate([clean, bad, marked])
    assert table["pages"] == 3
    assert table["pages_with_unmarked"] == 1
    assert table["unmarked_errors"] == 1
    assert table["marked_errors"] == 1
    assert table["errors_per_bad_page"] == 1.0
    assert table["by_kind"] == {"digit": 2}


def test_clopper_pearson_lower_satisfies_its_definition():
    bound = clopper_pearson_lower(8, 10, alpha=0.05)
    assert tail_probability(8, 10, bound) == pytest.approx(0.05, abs=1e-6)
    assert 0 < bound < 0.8


def test_clopper_pearson_lower_endpoints():
    assert clopper_pearson_lower(0, 60) == 0.0
    assert clopper_pearson_lower(10, 10) == pytest.approx(0.05**0.1, abs=1e-9)
    with pytest.raises(ValueError):
        clopper_pearson_lower(11, 10)


def test_clopper_pearson_lower_is_monotone_in_successes():
    bounds = [clopper_pearson_lower(k, 60) for k in range(0, 61, 5)]
    assert bounds == sorted(bounds)


def test_clopper_pearson_upper_is_the_dual_of_the_lower_bound():
    assert clopper_pearson_upper(0, 12) == pytest.approx(1.0 - clopper_pearson_lower(12, 12))
    assert clopper_pearson_upper(12, 12) == 1.0
    assert 0.0 < clopper_pearson_upper(3, 12) < 0.6
    assert clopper_pearson_upper(3, 12) < clopper_pearson_upper(6, 12)
    assert tail_probability(0, 12, clopper_pearson_upper(0, 12)) >= 0.0


def test_normalization_collapses_whitespace_and_expands_ligatures():
    assert normalize("the  margin\n\nis\tnot empty") == "the margin is not empty"
    assert normalize("con\uFB01rmation") == "confirmation"


def test_a_whitespace_only_difference_is_not_an_error_when_normalized():
    truth = "the margin is not empty space"
    read = "the margin  is not\nempty space\n"
    assert score_page(truth, read).errors > 0
    assert score_page(truth, read, normalized=True).errors == 0


def test_normalization_keeps_signs_decimals_and_accents_significant():
    assert normalize("\u22121.5%") == "\u22121.5%"
    assert score_page("naive and 1.5%", "naïve and -1.5%", normalized=True).errors > 0


def test_release_rule_matches_the_design():
    assert rule_of_three(60) == pytest.approx(0.05)
    assert release_bound(60, 0.5) == pytest.approx(0.10)
    assert release_allowed(60, 0.5, detections=0)
    assert not release_allowed(60, 0.5, detections=1)
    assert not release_allowed(60, 0.4, detections=0)
    assert release_allowed(60, 0.6, detections=0)
    assert release_bound(60, 0.0) == float("inf")
    assert not release_allowed(60, 0.0, detections=0)


class _ExactReader:
    """Reports every planted span and nothing else."""

    def __call__(self, seeded) -> list[Detection]:
        return [Detection(error.final_start, error.final_end) for error in seeded.errors]


class _BlindReader:
    def __call__(self, seeded) -> list[Detection]:
        return []


class _SloppyReader:
    """Catches only the real-word substitutions."""

    def __call__(self, seeded) -> list[Detection]:
        return [
            Detection(error.final_start, error.final_end)
            for error in seeded.errors
            if error.kind == "real_word"
        ]


def seeded_set():
    screens = build_corpus(dprs=(1,), kinds=("two_column_prose",))
    return build_seeded(screens)


def test_an_empty_seeded_set_has_no_lower_bound_and_refuses_release():
    """A population with no members is undefined, not perfect.

    The pipeline-shaped set on page-visible prose came out empty: the three
    readers agreed on no wrong word. Reporting a bound of 1.0 there would let a
    book be released on the strength of a measurement that never happened.
    """
    from ocr_synth.metrics import SensitivityResult

    empty = SensitivityResult(reader="none", planted=0, detected=0)
    assert empty.lower == 0.0
    assert empty.sensitivity == 0.0
    assert not release_allowed(60, empty.lower, detections=0)


def test_an_exact_auditor_has_sensitivity_one_and_a_bound_above_a_half():
    result = audit_sensitivity("exact", seeded_set(), _ExactReader())
    assert result.sensitivity == 1.0
    assert result.lower > 0.5, "a perfect reader on this set must clear the gate"
    assert result.misses == ()


def test_a_blind_auditor_has_no_lower_bound():
    result = audit_sensitivity("blind", seeded_set(), _BlindReader())
    assert result.sensitivity == 0.0
    assert result.lower == 0.0
    assert len(result.misses) == result.planted
    assert not release_allowed(60, result.lower, detections=0)


def test_a_partial_auditor_is_bounded_by_what_it_caught():
    result = audit_sensitivity("sloppy", seeded_set(), _SloppyReader())
    assert 0 < result.sensitivity < 1
    assert result.lower < result.sensitivity
    assert set(result.by_kind) == {"real_word", "digit", "omission", "insertion", "punctuation", "order"}
    assert result.by_kind["real_word"] == (3, 3)
    assert result.by_kind["digit"][1] == 0


def _one_seeded_page() -> SeededText:
    """Truth 'the form of it', candidate 'the farm of it': one substitution."""
    final = "the farm of it"
    return SeededText(
        truth="the form of it",
        final=final,
        errors=(
            PlantedError(
                kind="real_word",
                final_start=4,
                final_end=8,
                final_text="farm",
                truth_start=4,
                truth_end=8,
                truth_text="form",
                note="planted",
            ),
        ),
    )


def test_a_detection_must_read_the_truth_to_count_as_one():
    """Design 7.4, not span overlap: overlap alone is satisfied by noise.

    An auditor that reports one difference covering the whole page overlaps every
    planted span. Under overlap-only scoring it scores 1.0 and passes a gate it has
    learned nothing about; under the strict definition it must read the truth at
    the span, and this one reads the candidate back, so it finds nothing.
    """
    page = _one_seeded_page()

    def reports_everything(_page):
        return [Detection(0, len(_page.final), snippet=_page.final[:20], reading=_page.final)]

    loose = audit_sensitivity("pathological", [page], reports_everything)
    assert (loose.planted, loose.detected) == (1, 1), "overlap alone is trivially satisfied"

    strict = audit_sensitivity("pathological", [page], reports_everything, strict=True)
    assert (strict.planted, strict.detected) == (1, 0), "it never reads the truth"
    assert strict.lower == 0.0


def test_a_detection_that_reads_the_truth_counts_under_the_strict_definition():
    page = _one_seeded_page()

    def reads_the_truth(_page):
        return [Detection(4, 8, snippet="form", reading="form")]

    strict = audit_sensitivity("honest", [page], reads_the_truth, strict=True)
    assert (strict.planted, strict.detected) == (1, 1)

    def reads_something_else(_page):
        return [Detection(4, 8, snippet="firm", reading="firm")]

    other = audit_sensitivity("misread", [page], reads_something_else, strict=True)
    assert other.detected == 0, "a difference at the span is not a finding of the error"


def test_the_strict_definition_normalizes_the_reading_the_way_the_aligner_does():
    """NFC, ligatures expanded, whitespace collapsed; case and punctuation matter."""
    page = _one_seeded_page()

    spaced = audit_sensitivity(
        "spaced", [page], lambda _p: [Detection(4, 8, reading="  form  ")], strict=True
    )
    assert spaced.detected == 1, "whitespace is collapsed by the normalization"

    cased = audit_sensitivity(
        "cased", [page], lambda _p: [Detection(4, 8, reading="Form")], strict=True
    )
    assert cased.detected == 0, "case is significant in 7.4, so 'Form' is not the truth 'form'"
