"""Deciding whether re-chunking would actually change anything.

Making the token estimate script-aware changed chunker output only for
non-Latin text, but it bumped every chunker's version — which marked all
260,447 passages in the corpus stale. Re-chunking them would delete and
re-embed a quarter of a million passages in order to arrive at byte-identical
text under a different label: hours of GPU time, nothing a reader could see,
and the version-drift check unreadable until it finished.

The comparison is therefore on content, not on labels. These tests pin both
directions, because a comparison that always says "identical" would skip real
work and leave the corpus permanently stale — a far worse failure than the
wasted re-embed it is trying to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from research_engine.services.ingestion.reindex import _output_is_identical


@dataclass
class _Stored:
    """The fields of a stored passage the comparison looks at."""

    char_start: int
    char_end: int
    text: str
    token_count: int = 10
    chunker_version: str = "3.0"
    id: UUID = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.id is None:
            self.id = uuid4()


@dataclass
class _Draft:
    char_start: int
    char_end: int
    text: str
    token_count: int = 10
    chunker_version: str = "4.0"


def test_identical_spans_and_text_are_recognised_as_unchanged() -> None:
    old = [_Stored(0, 10, "the clerk "), _Stored(10, 20, "wrote it. ")]
    new = [_Draft(0, 10, "the clerk "), _Draft(10, 20, "wrote it. ")]

    assert _output_is_identical(old, new)


def test_a_changed_boundary_is_not_unchanged() -> None:
    """The direction that matters most: never skip real work."""
    old = [_Stored(0, 10, "the clerk "), _Stored(10, 20, "wrote it. ")]
    new = [_Draft(0, 12, "the clerk w"), _Draft(12, 20, "rote it. ")]

    assert not _output_is_identical(old, new)


def test_a_different_passage_count_is_not_unchanged() -> None:
    old = [_Stored(0, 20, "the clerk wrote it. ")]
    new = [_Draft(0, 10, "the clerk "), _Draft(10, 20, "wrote it. ")]

    assert not _output_is_identical(old, new)


def test_same_span_but_different_text_is_not_unchanged() -> None:
    """Guards the invariant the whole corpus rests on.

    Text that disagrees with its own offsets is the defect `doctor` calls
    critical, so it must never be waved through as "unchanged".
    """
    old = [_Stored(0, 10, "the clerk ")]
    new = [_Draft(0, 10, "the CLERK ")]

    assert not _output_is_identical(old, new)


def test_labels_alone_do_not_count_as_a_change() -> None:
    """The whole point: a version bump and a re-estimated token count are labels.

    Both differ here — `chunker_version` 3.0 vs 4.0, and a token count that the
    script-aware estimator revises — while every character sits at exactly the
    same offset.
    """
    old = [_Stored(0, 10, "λόγος ἦν ὁ", token_count=2, chunker_version="3.0")]
    new = [_Draft(0, 10, "λόγος ἦν ὁ", token_count=5, chunker_version="4.0")]

    assert _output_is_identical(old, new)


def test_an_empty_document_compares_equal() -> None:
    assert _output_is_identical([], [])
