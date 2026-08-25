"""Folding typography without losing the address of the raw characters.

The map is the whole point. A `normalized` match that cannot say *where* in the
source it matched is half an answer to someone who needs a page number.
"""

from __future__ import annotations

import pytest

from research_engine.services.text.normalize import (
    normalize_for_matching,
    normalize_with_map,
)


def _roundtrip(raw: str, needle: str) -> tuple[int, int]:
    """Locate *needle* in normalized space, return the raw span it maps to."""
    normalized, index_map = normalize_with_map(raw)
    target = normalize_for_matching(needle)
    at = normalized.find(target)
    assert at >= 0, f"{target!r} not in {normalized!r}"
    return index_map[at], index_map[at + len(target) - 1] + 1


class TestOffsetsSurviveFolding:
    def test_curly_quotes_map_back_to_the_curly_characters(self):
        raw = 'He said “justice and righteousness” loudly.'
        start, end = _roundtrip(raw, '"justice and righteousness"')
        assert raw[start:end] == '“justice and righteousness”'

    def test_collapsed_whitespace_maps_to_the_whole_run(self):
        raw = "mishpat   and\n\n  tsedaqah"
        start, end = _roundtrip(raw, "mishpat and tsedaqah")
        assert raw[start:end] == raw

    def test_linebreak_hyphenation_maps_across_the_break(self):
        raw = "a righ-\nteous ruler"
        start, end = _roundtrip(raw, "righteous ruler")
        assert raw[start:end] == "righ-\nteous ruler"

    def test_soft_hyphens_are_folded_away(self):
        raw = "judg­ment"
        assert normalize_for_matching(raw) == "judgment"
        start, end = _roundtrip(raw, "judgment")
        assert raw[start:end] == raw

    def test_em_dash_matches_a_typed_hyphen(self):
        raw = "justice—not mercy"
        start, end = _roundtrip(raw, "justice-not mercy")
        assert raw[start:end] == raw

    def test_ligature_expands_and_both_halves_point_at_one_character(self):
        raw = "the ﬁrst ruler"
        normalized, index_map = normalize_with_map(raw)
        assert "first" in normalized
        at = normalized.index("first")
        # 'f' and 'i' both came from the single ligature character.
        assert index_map[at] == index_map[at + 1] == raw.index("ﬁ")


class TestSymmetry:
    @pytest.mark.parametrize(
        "text",
        [
            "מִשְׁפָּט",  # pointed Hebrew
            "κρίσις",  # accented Greek
            "plain ascii",
        ],
    )
    def test_a_string_matches_itself_after_folding(self, text):
        """Both sides go through the same transform, so identity must survive.

        Whole-string NFKC would recompose base+combining pairs, which have no
        single raw offset; per-character NFKC does not. That only stays correct
        while the query is folded the same way.
        """
        normalized, index_map = normalize_with_map(text)
        assert normalize_for_matching(text) == normalized
        assert len(index_map) == len(normalized)

    def test_the_map_is_the_right_length_and_monotonic(self):
        raw = "He said  “justice”—a righ-\nteous word."
        normalized, index_map = normalize_with_map(raw)
        assert len(index_map) == len(normalized)
        assert index_map == sorted(index_map)
        assert all(0 <= i < len(raw) for i in index_map)
