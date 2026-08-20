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
from research_engine.services.text.dates import parse_fuzzy_date

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
