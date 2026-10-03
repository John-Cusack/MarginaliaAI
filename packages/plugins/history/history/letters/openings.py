"""Where each letter begins in its volume.

A `letter_opening` record is anchored to its passage: ``evidence_start`` is the
offset of its first evidence field — ``opening`` is declared first for exactly
this reason — within that passage's text. Passages are cut by chunk length and
overlap (232 pairs in Faraday vol 1, by a median 142 characters), so the same
heading can be read twice, from two passages, at two passage-relative offsets
that map to one place in the volume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Two readings this close, with the same dateline, are one opening read twice.
#: Whitespace-tolerant quote location can shift an offset by a few characters.
MERGE_DISTANCE = 8

_SPACE = re.compile(r"\s+")


def normalized(text: str | None) -> str:
    return _SPACE.sub(" ", text or "").strip().lower()


@dataclass
class Opening:
    """One letter's start, as read by the model and placed on the volume."""

    start: int
    record_ids: list[str]
    passage_id: str
    data: dict[str, Any]
    #: Every passage the opening was read from, when overlap read it twice.
    passage_ids: list[str] = field(default_factory=list)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def confidence(self) -> float:
        try:
            return float(self.data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return 0.0


@dataclass
class Placement:
    openings: list[Opening]
    excluded: int = 0
    merged: int = 0
    #: Records whose quotation is not at the offset they claim — a passage
    #: re-chunked since extraction, say. Reported, never placed by guesswork.
    misplaced: list[dict[str, Any]] = field(default_factory=list)


def place(
    records: list[dict[str, Any]],
    passages: dict[str, dict[str, Any]],
    text: str,
    excluded_ranges: list[tuple[int, int]] | None = None,
) -> Placement:
    """Turn records into openings at absolute offsets in *text*."""
    placed: list[Opening] = []
    misplaced: list[dict[str, Any]] = []
    excluded = 0
    for record in records:
        data = record.get("data") or {}
        passage = passages.get(str(record.get("passage_id")))
        opening_text = data.get("opening")
        if passage is None or passage.get("char_start") is None or not opening_text:
            misplaced.append({"record_id": record.get("id"), "why": "no placeable passage"})
            continue
        start = int(passage["char_start"]) + int(record.get("evidence_start") or 0)
        found = text[start : start + 2 * len(opening_text) + 16]
        if not normalized(found).startswith(normalized(opening_text)):
            misplaced.append(
                {
                    "record_id": record.get("id"),
                    "why": "the quotation is not at its offset in the volume's text",
                    "opening": opening_text,
                }
            )
            continue
        if any(lo <= start < hi for lo, hi in excluded_ranges or []):
            excluded += 1
            continue
        placed.append(
            Opening(
                start=start,
                record_ids=[str(record.get("id"))],
                passage_id=str(record.get("passage_id")),
                passage_ids=[str(record.get("passage_id"))],
                data=data,
            )
        )
    openings, merged = merge(placed)
    return Placement(openings=openings, excluded=excluded, merged=merged, misplaced=misplaced)


def merge(openings: list[Opening]) -> tuple[list[Opening], int]:
    """Collapse readings of one opening from overlapping passages.

    Same place (within `MERGE_DISTANCE`) and the same dateline, whitespace
    aside. Exact-offset equality alone under-merges: the same quotation located
    in two passages can land a few characters apart. The reading with the
    higher confidence is kept, and every record id travels with it.
    """
    ordered = sorted(openings, key=lambda o: o.start)
    kept: list[Opening] = []
    merged = 0
    for opening in ordered:
        last = kept[-1] if kept else None
        if (
            last is not None
            and opening.start - last.start <= MERGE_DISTANCE
            and normalized(opening.get("dateline")) == normalized(last.get("dateline"))
        ):
            merged += 1
            winner, loser = (opening, last) if opening.confidence > last.confidence else (last, opening)
            winner.record_ids = sorted(set(last.record_ids) | set(opening.record_ids))
            winner.passage_ids = sorted(set(last.passage_ids) | set(opening.passage_ids))
            winner.start = min(loser.start, winner.start)
            kept[-1] = winner
            continue
        kept.append(opening)
    return kept, merged
