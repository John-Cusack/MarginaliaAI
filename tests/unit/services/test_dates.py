"""Reading the dates people wrote in letters.

The cases that matter here are not "is this parser correct in general" but "does
it read nineteenth-century correspondence", which is what the corpus holds. The
epistolary forms — ult., inst., prox. — are the whole reason this exists: they
are complete dates to the man writing and meaningless without knowing when he
wrote, and a parser that guessed at them would put wrong dates in a timeline.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from research_engine.domain.common import DatePrecision
from research_engine.services.text.dates import (
    dominant_century,
    parse_fuzzy_date,
    scan_dates,
)

#: McClellan on the Peninsula. Any anchor would do; a real one keeps the
#: expected values readable.
ANCHOR = datetime(1862, 5, 20, tzinfo=UTC)


def day_of(text: str, anchor: datetime | None = None):
    result = parse_fuzzy_date(text, relative_to=anchor)
    assert result is not None, f"{text!r} did not parse"
    return result


class TestFullDates:
    @pytest.mark.parametrize(
        "text",
        [
            "May 15, 1862",
            "May 15th, 1862",
            "May 15th 1862",
            "15 May 1862",
            "15th May, 1862",
            "1862-05-15",
            "5/15/1862",
        ],
    )
    def test_the_same_day_written_seven_ways(self, text: str):
        result = day_of(text)
        assert result.start.date() == datetime(1862, 5, 15, tzinfo=UTC).date()
        assert result.precision == DatePrecision.day

    @pytest.mark.parametrize(
        ("text", "month"),
        [("Jany 3d, 1863", 1), ("Feby 3d, 1863", 2), ("Sept 3d, 1863", 9), ("Octr 3d, 1863", 10)],
    )
    def test_period_month_abbreviations(self, text: str, month: int):
        """`Jany` and `Feby` are how the century wrote them."""
        assert day_of(text).start.month == month

    @pytest.mark.parametrize("text", ["May 2d, 1862", "May 2nd, 1862", "May 3d, 1862", "May 3rd, 1862"])
    def test_period_ordinals(self, text: str):
        """`2d` and `3d` are as common as `2nd` and `3rd` in these letters."""
        assert day_of(text).precision == DatePrecision.day


class TestCoarserPrecision:
    def test_month_spans_the_month(self):
        result = day_of("May 1862")
        assert (result.start.day, result.end.day) == (1, 31)
        assert result.precision == DatePrecision.month

    def test_february_in_a_leap_year(self):
        assert day_of("February 1864").end.day == 29

    def test_year_spans_the_year(self):
        result = day_of("1862")
        assert (result.start.month, result.end.month) == (1, 12)
        assert result.precision == DatePrecision.year

    def test_season(self):
        result = day_of("spring of 1862")
        assert (result.start.month, result.end.month) == (3, 5)
        assert result.precision == DatePrecision.season

    def test_winter_runs_into_the_following_year(self):
        """"The winter of 1862" begins in December 1862 and ends in 1863."""
        result = day_of("winter of 1862")
        assert (result.start.year, result.start.month) == (1862, 12)
        assert (result.end.year, result.end.month) == (1863, 2)

    def test_decade(self):
        result = day_of("1860s")
        assert (result.start.year, result.end.year) == (1860, 1869)
        assert result.precision == DatePrecision.decade

    def test_week_of(self):
        result = day_of("week of May 15, 1862")
        assert (result.start.day, result.end.day) == (15, 21)
        assert result.precision == DatePrecision.week


class TestEpistolaryForms:
    """The conventions that make a letter's date readable at all."""

    def test_ultimo_is_the_previous_month(self):
        assert day_of("the 3d ult.", ANCHOR).start.date() == datetime(1862, 4, 3, tzinfo=UTC).date()

    def test_instant_is_the_current_month(self):
        assert day_of("the 15th inst.", ANCHOR).start.date() == datetime(1862, 5, 15, tzinfo=UTC).date()

    def test_proximo_is_the_next_month(self):
        assert day_of("the 2d prox.", ANCHOR).start.date() == datetime(1862, 6, 2, tzinfo=UTC).date()

    @pytest.mark.parametrize("text", ["the 3d ult.", "the 3d ulto", "the 3d ultimo"])
    def test_long_and_short_forms(self, text: str):
        assert day_of(text, ANCHOR).start.month == 4

    def test_a_lead_in_phrase_is_stripped(self):
        """A model carries the surrounding words in as often as not."""
        assert day_of("yours of the 15th inst.", ANCHOR).start.day == 15

    def test_a_bare_day_already_past_is_this_month(self):
        assert day_of("the 15th", ANCHOR).start.date() == datetime(1862, 5, 15, tzinfo=UTC).date()

    def test_a_bare_day_still_to_come_is_last_month(self):
        """Written on the 20th, "the 25th" cannot mean five days hence."""
        assert day_of("the 25th", ANCHOR).start.date() == datetime(1862, 4, 25, tzinfo=UTC).date()

    def test_a_month_without_a_year_is_the_most_recent_one(self):
        assert day_of("June 3d", ANCHOR).start.year == 1861

    def test_ultimo_crossing_a_year_boundary(self):
        january = datetime(1863, 1, 10, tzinfo=UTC)
        result = day_of("the 28th ult.", january)
        assert (result.start.year, result.start.month) == (1862, 12)


class TestRefusals:
    """A wrong date in a timeline is worse than a gap, because a gap is visible."""

    def test_a_relative_form_without_an_anchor_is_refused(self):
        assert parse_fuzzy_date("the 3d ult.") is None

    def test_a_bare_day_without_an_anchor_is_refused(self):
        assert parse_fuzzy_date("the 15th") is None

    @pytest.mark.parametrize("text", ["your last", "some time ago", "recently", ""])
    def test_a_phrase_that_is_not_a_date_is_refused(self, text: str):
        assert parse_fuzzy_date(text, relative_to=ANCHOR) is None

    def test_an_impossible_day_is_refused(self):
        """The 31st of a thirty-day month is a transcription or OCR error."""
        assert parse_fuzzy_date("April 31, 1862") is None

    def test_an_impossible_month_is_refused(self):
        assert parse_fuzzy_date("1862-13-01") is None

    def test_none_and_whitespace(self):
        assert parse_fuzzy_date("") is None
        assert parse_fuzzy_date("   ") is None


class TestScanningScannedText:
    """Finding a dateline inside a page that has been through OCR.

    Measured against the 728 letters of Sears's edition of McClellan's papers:
    91% carry a date this finds, and 0.8% of those fall out of the edition's own
    chronological order — the check that catches a date picked out of the body
    rather than the dateline.
    """

    def test_a_plain_dateline(self):
        [(_, _, date)] = scan_dates("Cincinnati, April 24, 1861")
        assert date.start.date() == datetime(1861, 4, 24, tzinfo=UTC).date()

    def test_no_comma_between_place_and_date(self):
        [(_, _, date)] = scan_dates("Head Quarters OVM Cincinnati April 29 1861")
        assert date.start.day == 29

    def test_a_year_the_editor_supplied(self):
        [(_, _, date)] = scan_dates("Cincinnati Dec 27 [1860]")
        assert date.start.year == 1860

    def test_a_month_the_scanner_misread(self):
        """"Dee" for "Dec" shares two letters with its target."""
        [(_, _, date)] = scan_dates("Cincinnati Dee 27 [1860]")
        assert date.start.month == 12

    def test_a_month_misread_no_one_listed(self):
        """The similarity fallback, for misreads nobody enumerated."""
        [(_, _, date)] = scan_dates("Camp near Sharpsburg, Marcli 24, 1862")
        assert date.start.month == 3

    def test_a_slashed_short_year_needs_a_century(self):
        assert scan_dates("Cincinnati April 18/61") == []
        [(_, _, date)] = scan_dates("Cincinnati April 18/61", century=1800)
        assert date.start.year == 1861

    def test_the_month_and_day_split_by_a_bracket(self):
        [(_, _, date)] = scan_dates("[Washington, August] 16th [1861]")
        assert date.start.date() == datetime(1861, 8, 16, tzinfo=UTC).date()

    def test_a_telegram_time_is_not_a_year(self):
        """The defect this cost the most to find.

        A telegram dateline puts the hour exactly where a two-digit year would
        go, so "March 24 11 am 1862" read as 1811 — and 26 letters landed in the
        1810s, before McClellan was born. A four-digit year just past the day
        wins over anything shorter.
        """
        [(_, _, date)] = scan_dates(
            "Head Quarters, Army of the Potomac, Seminary March 24 11 am 1862"
        )
        assert date.start.year == 1862
        assert date.start.month == 3

    @pytest.mark.parametrize(
        "line",
        [
            "Near Yorktown April 11 12.30 am 1862",
            "Berkeley August 4 12m 1862",
            "Camp Lincoln June 14, 11 a.m. 1862",
        ],
    )
    def test_other_telegram_times(self, line: str):
        [(_, _, date)] = scan_dates(line)
        assert date.start.year == 1862

    def test_an_editor_s_alternative_day_is_stepped_over(self):
        """"Aug 9 [10] 1861" — the editor offering a second reading of the day."""
        [(_, _, date)] = scan_dates("## Washington Aug 9 [10] 1861 1 am.")
        assert date.start.year == 1861

    def test_dates_come_back_in_document_order(self):
        found = scan_dates("May 3, 1862 ... June 4, 1862 ... July 5, 1862")
        assert [d.start.month for _, _, d in found] == [5, 6, 7]

    def test_the_span_addresses_the_text_it_read(self):
        text = "Camp near Sharpsburg, Sept. 20, 1862 — my dear Nelly"
        [(start, end, _)] = scan_dates(text)
        assert text[start:end].strip().rstrip(",") == "Sept. 20, 1862"

    def test_ordinary_prose_yields_nothing(self):
        assert scan_dates("I have 3 brigades and 4 batteries in the field.") == []


class TestDominantCentury:
    def test_read_from_the_years_the_text_states(self):
        assert dominant_century("1861 1862 1862 1863 1864 1865") == 1800

    def test_refused_when_there_is_too_little_to_go_on(self):
        assert dominant_century("1862") is None

    def test_refused_when_the_text_is_split_between_centuries(self):
        assert dominant_century("1861 1862 1863 1961 1962 1963 2001 2002") is None


class TestPhrasesTheLettersActuallyUse:
    """Read off real passages of the Sears edition, not invented.

    Sampling four letters before paying for an extraction run turned up three
    forms the parser could not read. Every one of them is common in the volume.
    """

    @pytest.mark.parametrize(
        ("phrase", "day"),
        [
            # "Yours of the 2nd has reached me" — with the ordinal.
            ("yours of the 2nd", 2),
            # "your letters of the 19 & 20" — without it. Both forms appear,
            # sometimes in the same letter.
            ("your letters of the 19", 19),
            ("the 19", 19),
            ("your communication of the 14th inst", 14),
            ("your confidential letter of the 23rd", 23),
            ("your very kind letter of the 3d", 3),
        ],
    )
    def test_a_letter_referred_to_by_day(self, phrase: str, day: int):
        assert day_of(phrase, ANCHOR).start.day == day

    def test_of_today(self):
        """"Your telegram of today is received." """
        assert day_of("your telegram of today", ANCHOR).start.date() == ANCHOR.date()

    def test_of_yesterday(self):
        """"Your note of yesterday is received." """
        result = day_of("your note of yesterday", ANCHOR)
        assert result.start.day == ANCHOR.day - 1

    def test_yesterday_across_a_month_boundary(self):
        first = datetime(1862, 3, 1, tzinfo=UTC)
        result = day_of("yesterday", first)
        assert (result.start.month, result.start.day) == (2, 28)

    def test_a_bare_day_still_needs_an_anchor(self):
        """Relaxing the ordinal must not make a bare number resolvable alone."""
        assert parse_fuzzy_date("the 19") is None
