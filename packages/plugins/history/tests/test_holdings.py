"""Deciding whether a referenced letter is one the corpus already holds.

The distinction these tests exist to protect is between three answers, not two:
found, absent, and *not checkable*. Collapsing the third into the second is how
a parsing limitation becomes a fabricated archival discovery.
"""

from __future__ import annotations

import pytest
from history.tools._holdings import (
    RECEIVED,
    SENT,
    UNKNOWN,
    Holdings,
    direction_of,
    surname,
)


class TestSurname:
    @pytest.mark.parametrize(
        "written",
        [
            "Stanton",
            "Edwin M. Stanton",
            "Hon E M Stanton Secty of War",
            "Hon. Edwin M. Stanton, Secretary of War",
        ],
    )
    def test_one_man_written_four_ways(self, written: str):
        """Rank and office differ between variants; the surname does not."""
        assert surname(written) == "stanton"

    def test_an_editor_s_reference_line_is_not_a_name(self):
        assert surname("GBM to Andrew Porter") == "porter"

    @pytest.mark.parametrize("written", ["you", "him", "the President", "Genl", ""])
    def test_things_that_are_not_a_person(self, written: str):
        assert surname(written) is None

    def test_none(self):
        assert surname(None) is None


class TestDirection:
    def test_the_author_s_own_letters(self):
        assert direction_of("prior_letter") == SENT
        assert direction_of("enclosure") == SENT

    def test_letters_to_the_author(self):
        assert direction_of("received_letter") == RECEIVED

    def test_everything_else(self):
        assert direction_of("mentioned_letter") == UNKNOWN
        assert direction_of(None) == UNKNOWN


class TestHoldings:
    def a_corpus(self) -> Holdings:
        holdings = Holdings()
        holdings.add("To Henry W. Halleck", "1862-10-25T00:00:00+00:00")
        holdings.add("To Winfield Scott", "1861-04-27T00:00:00+00:00")
        return holdings

    def test_a_letter_we_hold_is_found(self):
        assert self.a_corpus().held("halleck", "1862-10-25") == "To Henry W. Halleck"

    def test_matching_is_by_day_not_by_timestamp(self):
        assert self.a_corpus().held("halleck", "1862-10-25T14:30:00+00:00")

    def test_the_right_man_on_the_wrong_day_is_not_a_match(self):
        assert self.a_corpus().held("halleck", "1862-10-26") is None

    def test_the_wrong_man_on_the_right_day_is_not_a_match(self):
        assert self.a_corpus().held("stanton", "1862-10-25") is None

    def test_no_date_means_no_lookup(self):
        """Not a miss — a question that cannot be asked."""
        assert self.a_corpus().held("halleck", None) is None

    def test_an_undated_section_is_not_indexed(self):
        holdings = Holdings()
        holdings.add("To Someone", None)
        assert holdings.total == 0


class TestCoverageNote:
    def test_an_outgoing_edition_says_so_once(self):
        """Otherwise every inbound reference reads as a discovery."""
        holdings = Holdings()
        for day in range(1, 4):
            holdings.add("To Halleck", f"1862-10-0{day}T00:00:00+00:00")

        note = holdings.coverage_note()
        assert note is not None
        assert "none received by him" in note

    def test_an_incoming_edition_says_the_converse(self):
        holdings = Holdings()
        holdings.add("From Halleck", "1862-10-01T00:00:00+00:00")

        assert "none sent by him" in holdings.coverage_note()

    def test_a_corpus_holding_both_directions_needs_no_caveat(self):
        holdings = Holdings()
        holdings.add("To Halleck", "1862-10-01T00:00:00+00:00")
        holdings.add("From Halleck", "1862-10-02T00:00:00+00:00")

        assert holdings.coverage_note() is None

    def test_titles_that_state_no_direction(self):
        holdings = Holdings()
        holdings.add("Memorandum for the President", "1862-10-01T00:00:00+00:00")

        assert holdings.undirected == 1
        assert holdings.coverage_note() is None
