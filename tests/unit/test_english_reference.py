"""`unmapped` is not `same`, and the difference is the whole of this module.

`core.verse_map` is created by migration 016 and filled by
`scripts/load_versification.py`. A deploy that runs the migration and forgets
the load leaves the table empty — and because a verse the two traditions agree
on legitimately has *no row*, an empty map looked exactly like universal
agreement. `find_lemma` would have reported `mapping: "same"` for all 1,978
verses the English tradition actually moves, confidently and silently.

These pin the three outcomes apart. They are unit tests because the conflation
lived in a pure decision, not in the SQL.
"""

from __future__ import annotations

import pytest

from research_engine.services.words import english_reference

pytestmark = pytest.mark.unit


class TestWithNoMapLoaded:
    """No map is not an answer, and must not be dressed as one."""

    def test_a_verse_reports_unmapped_rather_than_same(self):
        assert english_reference(
            "Gen.18.19", None, None, None, None, map_loaded=False
        ) == {"ref": None, "mapping": "unmapped", "part": None, "hebrew_part": None}

    def test_the_english_reference_is_withheld_not_guessed(self):
        """Echoing the Hebrew reference back would be a claim, and a wrong one.

        `Ps.36.6` is `Ps.36.5` in English. Returning `Ps.36.6` as the English
        reference because no row was found is precisely the failure.
        """
        assert english_reference("Ps.36.6", None, None, None, None, map_loaded=False)[
            "ref"
        ] is None

    def test_it_does_not_matter_what_the_row_said(self):
        """Nothing can be mapped when there is no map, even if a row leaks in."""
        assert (
            english_reference(
                "Ps.36.6", "Ps.36.5", None, None, "full", map_loaded=False
            )["mapping"]
            == "unmapped"
        )


class TestWithTheMapLoaded:
    def test_a_verse_with_no_row_is_genuinely_the_same_verse(self):
        assert english_reference(
            "Gen.18.19", None, None, None, None, map_loaded=True
        ) == {
            "ref": "Gen.18.19",
            "mapping": "same",
            "part": None,
            "hebrew_part": None,
        }

    def test_a_moved_verse_reports_where_it_moved(self):
        assert english_reference(
            "Ps.36.6", "Ps.36.5", None, None, "full", map_loaded=True
        ) == {"ref": "Ps.36.5", "mapping": "full", "part": None, "hebrew_part": None}

    def test_a_partial_keeps_both_halves(self):
        """`Isa.63.19!b -> Isa.64.1`: the half is the reason it is not `full`."""
        assert english_reference(
            "Isa.63.19", "Isa.64.1", None, "b", "partial", map_loaded=True
        ) == {
            "ref": "Isa.64.1",
            "mapping": "partial",
            "part": None,
            "hebrew_part": "b",
        }


def test_the_three_outcomes_are_distinguishable():
    """A caller must be able to tell all three apart from `mapping` alone."""
    outcomes = {
        english_reference("Gen.1.1", None, None, None, None, map_loaded=False)["mapping"],
        english_reference("Gen.1.1", None, None, None, None, map_loaded=True)["mapping"],
        english_reference("Ps.36.6", "Ps.36.5", None, None, "full", map_loaded=True)[
            "mapping"
        ],
    }
    assert outcomes == {"unmapped", "same", "full"}
