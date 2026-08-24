"""Turn cross-encoder scores into a ranking.

Shared by the local and remote rerankers so the two cannot disagree about
ordering. They previously would have: each would have written its own sort, and
a tie-breaking difference between them is invisible in every test that only
checks the top result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID


def rank_from_scores(
    passage_ids: Sequence[UUID], scores: Sequence[float], k: int
) -> list[tuple[UUID, float]]:
    """Order passages by score, highest first, keeping the top *k*.

    Ties keep their input order. That matters more than it looks: the input
    arrives in fusion order, so an exact tie between two passages falls back to
    what RRF thought — a real signal — rather than to whatever order the ids
    happened to hash into. Python's sort is stable, so this is free, but it is
    load-bearing rather than incidental.
    """
    if len(passage_ids) != len(scores):
        raise ValueError(
            f"Rerank returned {len(scores)} scores for {len(passage_ids)} "
            f"passages. Scores are positional, so a length mismatch means the "
            f"pairing is wrong and every result would be mis-attributed."
        )
    ranked = sorted(
        zip(passage_ids, (float(s) for s in scores), strict=True),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return ranked[:k]
