"""Locating an old passage's text inside a document's canonical text.

Re-anchoring by *offset overlap* is the obvious algorithm and it is unavailable
here: passages written by `prose_window` 1.0 carry `byte_start: 0`, so there are
no valid old offsets to overlap against. Re-anchoring has to go through text
matching instead.

That is tractable because the damage is known and bounded. `prose_window` 1.0
rebuilt each chunk as ``" ".join(sentences)``, which collapses whitespace runs
and changes nothing else. Collapse whitespace on both sides and the old passage
text becomes an exact substring of the canonical text.
"""

from __future__ import annotations

from typing import NamedTuple


class Span(NamedTuple):
    start: int
    end: int

    @property
    def width(self) -> int:
        return self.end - self.start

    def overlap(self, other: Span) -> int:
        """Characters shared with *other*; 0 when disjoint."""
        return max(0, min(self.end, other.end) - max(self.start, other.start))


def collapse_whitespace_with_map(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs, keeping a map back to raw offsets.

    Returns ``(collapsed, index_map)`` where ``index_map[i]`` is the offset in
    *text* of ``collapsed[i]``. Each whitespace run becomes a single space
    mapped to the run's first character.
    """
    out: list[str] = []
    index_map: list[int] = []
    in_whitespace = False

    for i, ch in enumerate(text):
        if ch.isspace():
            if not in_whitespace:
                out.append(" ")
                index_map.append(i)
                in_whitespace = True
        else:
            out.append(ch)
            index_map.append(i)
            in_whitespace = False

    return "".join(out), index_map


def collapse_whitespace(text: str) -> str:
    collapsed, _ = collapse_whitespace_with_map(text)
    return collapsed


class CanonicalIndex:
    """A document's canonical text, prepared for repeated substring lookups."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.collapsed, self._map = collapse_whitespace_with_map(text)

    def find(self, needle: str, from_offset: int = 0) -> Span | None:
        """Locate *needle* in the canonical text, ignoring whitespace differences.

        *from_offset* is a raw offset hint: search resumes from there, so a
        passage repeated verbatim in a document resolves to successive
        occurrences rather than all to the first. Falls back to a search from the
        beginning when nothing is found after the hint.
        """
        target = collapse_whitespace(needle).strip()
        if not target:
            return None

        start_at = self._collapsed_offset_at_or_after(from_offset)
        found = self.collapsed.find(target, start_at)
        if found < 0 and start_at > 0:
            found = self.collapsed.find(target)
        if found < 0:
            return None

        raw_start = self._map[found]
        raw_end = self._map[found + len(target) - 1] + 1
        return Span(raw_start, raw_end)

    def _collapsed_offset_at_or_after(self, raw_offset: int) -> int:
        if raw_offset <= 0:
            return 0
        lo, hi = 0, len(self._map)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._map[mid] < raw_offset:
                lo = mid + 1
            else:
                hi = mid
        return lo


def best_overlap(span: Span, candidates: list[tuple[object, Span]]) -> object | None:
    """The candidate sharing the most characters with *span*.

    Ties break toward the candidate that starts earlier, so the choice is
    deterministic across runs — a re-anchor that shuffles on re-run would make
    the orphan report meaningless.
    """
    best_key: object | None = None
    best_score = 0
    best_start = 0
    for key, candidate in candidates:
        score = span.overlap(candidate)
        if score > best_score or (
            score == best_score and score > 0 and candidate.start < best_start
        ):
            best_key, best_score, best_start = key, score, candidate.start
    return best_key
