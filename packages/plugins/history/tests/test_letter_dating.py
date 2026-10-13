"""Each letter's date, or the reason it is held — against real datelines.

The rule behind every case: a wrong date on a timeline is worse than a gap,
because a gap is visible. So two readers must agree, a year must be printed to
be believed, a yearless date must be bracketed, and a date far from its
neighbours is held — never corrected.
"""

from __future__ import annotations

from datetime import date

import pytest
from history.letters import dating
from history.letters.dating import Reading, decide
from letter_fixtures import (
    AMPERE,
    APRIL_9_1813,
    BRUSSELS_1815,
    GENEVA_AUGUST_6,
    GENEVA_JULY_1,
    KEY_NOTE,
    MARINO,
    ROME_1816,
    ROME_APRIL_1814,
    ROME_FEBRUARY_1815,
    ROME_JANUARY_1815,
    THIRD_TO_ABBOTT,
    WOLLASTON,
    opening_data,
)


def decided(*records, tolerance=180, **kwargs):
    return decide([Reading.of(r) for r in records], tolerance_days=tolerance, **kwargs)


class TestPrintedYears:
    def test_a_clean_letter_head(self):
        """Marino, Sunday 28 July 1822: readers agree, the weekday agrees."""
        [marino] = decided(MARINO)
        assert marino.materialized
        assert marino.day.start == date(1822, 7, 28)
        assert marino.readers == "agree"
        assert marino.flags == []
        assert marino.confidence == 0.9

    def test_a_weekday_slip_is_kept_and_capped(self):
        """9 April 1813 was a Friday; Faraday wrote Thursday. A writer's slip."""
        [letter] = decided(APRIL_9_1813)
        assert letter.materialized
        assert "weekday_mismatch" in letter.flags
        assert letter.confidence == dating.CAP_WEEKDAY_MISMATCH

    def test_only_the_model_can_read_french(self):
        [ampere] = decided(AMPERE)
        assert ampere.materialized
        assert ampere.day.start == date(1830, 10, 13)
        assert ampere.readers == "single"
        assert ampere.confidence == dating.CAP_SINGLE_READER

    def test_strict_mode_holds_a_single_reader(self):
        [ampere] = decided(AMPERE, strict_single_reader=True)
        assert ampere.hold == dating.SINGLE_READER_STRICT
        assert ampere.candidate.start == date(1830, 10, 13)

    def test_an_editor_s_dating_is_capped(self):
        [third] = decided(THIRD_TO_ABBOTT)
        assert third.materialized
        assert third.date_source == "editorial"
        assert third.confidence == dating.CAP_EDITORIAL

    def test_readers_that_disagree_are_held(self):
        disagreeing = opening_data(
            "FAEADAT TO ABBOTT.",
            dateline="'Eome: Saturday, November 26, 1814.",
            date_written="November 25, 1814",
        )
        [letter] = decided(disagreeing)
        assert letter.hold == dating.READER_DISAGREEMENT
        assert letter.candidate.start == date(1814, 11, 25)

    def test_a_year_that_is_not_printed_is_held(self):
        """The model supplied 1814 for "Geneva : July 1." — from context."""
        invented = opening_data(
            "FARADAY TO HIS MOTHER.",
            dateline="' Geneva : July 1. Received July 18.",
            date_written="July 1, 1814",
        )
        [letter] = decided(invented)
        assert letter.hold == dating.HALLUCINATED_YEAR


class TestNoDate:
    def test_an_undated_note(self):
        [note] = decided(KEY_NOTE)
        assert note.hold == dating.UNDATED
        assert note.day is None

    def test_an_editor_s_conjecture(self):
        [letter] = decided(WOLLASTON)
        assert letter.hold == dating.CONJECTURAL


class TestYearlessDatelines:
    """Geneva : July 1. — between Rome, 14 April 1814 and Geneva, 6 August."""

    def test_forward_from_the_letter_before_and_bracketed_by_the_one_after(self):
        _, geneva, _ = decided(ROME_APRIL_1814, GENEVA_JULY_1, GENEVA_AUGUST_6)
        assert geneva.materialized
        assert geneva.day.start == date(1814, 7, 1), "not 1813: forward, not back"
        assert geneva.year_inferred_from == ["previous_letter", "next_letter"]
        assert geneva.confidence == 0.7, "the record's own 0.7 under the 0.75 cap"

    def test_the_receipt_note_is_read_forward_from_the_letter(self):
        _, geneva, _ = decided(ROME_APRIL_1814, GENEVA_JULY_1, GENEVA_AUGUST_6)
        assert geneva.received.start == date(1814, 7, 18)

    def test_a_receipt_crosses_the_new_year(self):
        rome = opening_data(
            "FAEADAT TO HIS MOTHER.",
            dateline="' Rome : November 10, 1814. Eeceived January 17.",
            date_written="November 10, 1814",
            received_date="January 17",
        )
        [letter] = decided(rome)
        assert letter.received.start == date(1815, 1, 17)

    def test_with_nothing_after_it_the_year_is_unbracketed(self):
        _, geneva = decided(ROME_APRIL_1814, GENEVA_JULY_1)
        assert geneva.hold == dating.YEAR_UNBRACKETED

    def test_a_weekday_that_does_not_fit_the_inferred_year_is_held(self):
        wrong_day = opening_data(
            "FARADAY TO HIS MOTHER.",
            dateline="' Geneva : Monday, July 1.",
            date_written="July 1",
            weekday="monday",
        )
        _, geneva, _ = decided(ROME_APRIL_1814, wrong_day, GENEVA_AUGUST_6)
        assert geneva.hold == dating.WEEKDAY_CONFLICT
        assert geneva.candidate.start == date(1814, 7, 1)


class TestChronology:
    TOUR = (ROME_JANUARY_1815, ROME_1816, ROME_FEBRUARY_1815, BRUSSELS_1815)

    def test_a_date_far_from_its_neighbours_is_held_not_corrected(self):
        """r.n23 prints 1816 between letters of January and February 1815."""
        _, rome_1816, _, _ = decided(*self.TOUR, tolerance=180)
        assert rome_1816.hold == dating.CHRONOLOGY_CONFLICT
        assert rome_1816.candidate.start == date(1816, 2, 13), "kept as printed"
        assert rome_1816.deviation_days > 300

    def test_holding_one_letter_leaves_its_neighbours_alone(self):
        decisions = decided(*self.TOUR, tolerance=180)
        assert [d.materialized for d in decisions] == [True, False, True, True]

    def test_without_a_tolerance_nothing_is_held_for_chronology(self):
        decisions = decided(*self.TOUR, tolerance=None)
        assert all(d.materialized for d in decisions)
        assert decisions[1].deviation_days > 300


class TestReview:
    def test_a_reviewer_s_date_wins(self):
        _, rome_1816, _, _ = decided(
            *TestChronology.TOUR,
            tolerance=180,
            reviewed={1: dating.iso_day("1815-02-13")},
        )
        assert rome_1816.materialized
        assert rome_1816.day.start == date(1815, 2, 13)
        assert "reviewed" in rome_1816.flags
        assert rome_1816.date_source == "reviewer"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1815-02-13", ("1815-02-13", "1815-02-13", "day")),
        ("1815-02", ("1815-02-01", "1815-02-28", "month")),
        ("1815", ("1815-01-01", "1815-12-31", "year")),
    ],
)
def test_a_reviewer_may_give_a_day_month_or_year(raw, expected):
    day = dating.iso_day(raw)
    assert (day.start.isoformat(), day.end.isoformat(), day.precision) == expected


@pytest.mark.parametrize("raw", ["", "1815-13", "13 Feb 1815", "1815-02-30"])
def test_nonsense_is_not_a_reviewed_date(raw):
    assert dating.iso_day(raw) is None


def test_titles_read_the_date():
    assert dating.format_day(dating.iso_day("1814-07-01")) == "1 July 1814"
    assert dating.format_day(dating.iso_day("1814-07")) == "July 1814"
    assert dating.format_day(None) == "undated"


class TestChronologyWindow:
    """In order means between the letters around it, not near their median."""

    def test_the_first_letter_of_a_tour_is_in_order(self):
        """Rome, April 1814 opens the sequence; its successors are all later."""
        decisions = decided(
            ROME_APRIL_1814, GENEVA_AUGUST_6, ROME_1816, ROME_FEBRUARY_1815, tolerance=180
        )
        assert decisions[0].materialized
        assert decisions[0].deviation_days == 0

    def test_a_sparse_stretch_is_in_order(self):
        """Letters years apart, printed in order, are not out of place."""
        sparse = [
            opening_data("TO A.", dateline=f"' London : {when}.", date_written=when)
            for when in (
                "July 25, 1817",
                "November 25, 1817",
                "February 27, 1818",
                "October 6, 1818",
                "February 12, 1821",
                "May 5, 1821",
                "October 8, 1821",
            )
        ]
        assert all(d.materialized for d in decided(*sparse, tolerance=180))

    def test_a_misread_year_among_its_true_neighbours_is_held(self):
        """r.n32 reads December 31, 1810 between letters of 1816 and 1817."""
        around = [
            opening_data("TO A.", dateline=f"' London : {when}.", date_written=when)
            for when in ("January 10, 1816", "February 9, 1816", "September 23, 1816")
        ]
        misread = opening_data(
            "' Dear A.", dateline="'December 31, 1810.", date_written="December 31, 1810"
        )
        after = [
            opening_data("TO A.", dateline=f"' London : {when}.", date_written=when)
            for when in ("January 20, 1817", "June 9, 1817", "June 27, 1817")
        ]
        decisions = decided(*around, misread, *after, tolerance=180)
        assert decisions[3].hold == dating.CHRONOLOGY_CONFLICT
        assert all(d.materialized for i, d in enumerate(decisions) if i != 3)
