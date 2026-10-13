"""`extraction run --exclude-range`: a volume's catalogue is not correspondence.

Bence Jones's *Life and Letters of Faraday* ends in 123,000 characters of the
Longmans publisher's catalogue. Extracting letter openings from it costs money
and invites the model to find letters in book advertisements.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from research_engine.cli.extraction import overlaps_excluded, parse_exclude_ranges

VOLUME = uuid4()


def test_a_range_names_its_document():
    assert parse_exclude_ranges([f"{VOLUME}:646185-769155"], None) == {
        VOLUME: [(646185, 769155)]
    }


def test_one_document_need_not_be_named():
    assert parse_exclude_ranges(["0-8833"], [VOLUME]) == {VOLUME: [(0, 8833)]}


def test_with_several_documents_it_must_be():
    with pytest.raises(ValueError, match="names no document"):
        parse_exclude_ranges(["0-8833"], [VOLUME, uuid4()])


@pytest.mark.parametrize("value", ["8833-0", "a-b", "12"])
def test_nonsense_is_refused(value):
    with pytest.raises(ValueError):
        parse_exclude_ranges([value], [VOLUME])


def test_overlap_is_any_shared_character():
    excluded = {VOLUME: [(100, 200)]}
    assert overlaps_excluded(VOLUME, 150, 250, excluded)
    assert overlaps_excluded(VOLUME, 50, 101, excluded)
    assert not overlaps_excluded(VOLUME, 200, 300, excluded)
    assert not overlaps_excluded(uuid4(), 150, 250, excluded)
    assert not overlaps_excluded(VOLUME, None, None, excluded)
