"""Locating old passage text in canonical text."""

from __future__ import annotations

import pytest

from research_engine.services.text.anchoring import (
    CanonicalIndex,
    Span,
    best_overlap,
    collapse_whitespace_with_map,
)
from research_engine.services.text.normalize import normalize

pytestmark = pytest.mark.unit


class TestCollapseMap:
    def test_map_points_at_the_original_offsets(self) -> None:
        text = "a  b\n\nc"
        collapsed, index_map = collapse_whitespace_with_map(text)
        assert collapsed == "a b c"
        assert [text[i] for i in index_map] == ["a", " ", "b", "\n", "c"]

    def test_each_whitespace_run_maps_to_its_first_character(self) -> None:
        text = "x\n\n\n\ny"
        collapsed, index_map = collapse_whitespace_with_map(text)
        assert collapsed == "x y"
        assert index_map == [0, 1, 5]

    def test_map_has_one_entry_per_collapsed_character(self) -> None:
        text = "  lots   of \t\n whitespace  "
        collapsed, index_map = collapse_whitespace_with_map(text)
        assert len(collapsed) == len(index_map)


class TestCanonicalIndex:
    RAW = "First sentence here.\n\nSecond sentence follows.   Third one ends it."

    def test_finds_text_whose_whitespace_was_collapsed(self) -> None:
        """The exact damage prose_window 1.0 did: `" ".join(sentences)`."""
        index = CanonicalIndex(self.RAW)
        old_text = " ".join(self.RAW.split())
        span = index.find(old_text)
        assert span is not None
        assert self.RAW[span.start : span.end] == self.RAW

    def test_span_addresses_the_raw_text(self) -> None:
        index = CanonicalIndex(self.RAW)
        span = index.find("Second sentence follows.")
        assert self.RAW[span.start : span.end] == "Second sentence follows."

    def test_span_across_a_collapsed_run(self) -> None:
        index = CanonicalIndex(self.RAW)
        span = index.find("follows. Third one")
        assert self.RAW[span.start : span.end] == "follows.   Third one"

    def test_missing_text_returns_none(self) -> None:
        assert CanonicalIndex(self.RAW).find("nowhere in this document") is None

    def test_empty_needle_returns_none(self) -> None:
        assert CanonicalIndex(self.RAW).find("   ") is None

    def test_repeated_text_resolves_to_successive_occurrences(self) -> None:
        """Two identical passages must not both anchor to the first occurrence."""
        raw = "Repeat me. Filler in between. Repeat me. Trailing text."
        index = CanonicalIndex(raw)

        first = index.find("Repeat me.")
        second = index.find("Repeat me.", first.start + 1)

        assert first.start != second.start
        assert raw[second.start : second.end] == "Repeat me."

    def test_hint_beyond_the_last_match_falls_back_to_the_start(self) -> None:
        raw = "Only once here. And more text after it."
        index = CanonicalIndex(raw)
        span = index.find("Only once here.", from_offset=len(raw) - 1)
        assert raw[span.start : span.end] == "Only once here."


class TestBestOverlap:
    def test_picks_the_greatest_overlap(self) -> None:
        target = Span(10, 20)
        candidates = [("a", Span(0, 12)), ("b", Span(8, 25)), ("c", Span(21, 30))]
        assert best_overlap(target, candidates) == "b"

    def test_returns_none_when_nothing_overlaps(self) -> None:
        assert best_overlap(Span(10, 20), [("a", Span(30, 40))]) is None

    def test_ties_break_deterministically_toward_the_earlier_span(self) -> None:
        target = Span(10, 20)
        candidates = [("late", Span(15, 25)), ("early", Span(5, 15))]
        assert best_overlap(target, candidates) == "early"
        assert best_overlap(target, list(reversed(candidates))) == "early"

    def test_touching_spans_do_not_count_as_overlapping(self) -> None:
        assert best_overlap(Span(10, 20), [("a", Span(20, 30))]) is None


class TestNormalize:
    def test_curly_quotes_and_dashes_fold(self) -> None:
        assert normalize("“Quoted” — dash") == '"Quoted" - dash'

    def test_soft_hyphens_are_removed(self) -> None:
        assert normalize("hy­phen") == "hyphen"

    def test_line_break_hyphenation_rejoins(self) -> None:
        assert normalize("fis-\ncal policy") == "fiscal policy"

    def test_real_compounds_keep_their_hyphen(self) -> None:
        """`Anglo-\\nSaxon` is a hyphenated word, not a broken one."""
        assert normalize("Anglo-\nSaxon") == "Anglo- Saxon"

    def test_ligatures_fold_via_nfkc(self) -> None:
        assert normalize("ﬁnal oﬃce") == "final office"

    def test_whitespace_collapses(self) -> None:
        assert normalize("a\n\n  b\t\tc") == "a b c"
