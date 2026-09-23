"""Retrieval metrics.

`08-search-and-extraction.md:211` and `11-implementation-architecture.md:1504`
have specified recall@10, MRR and nDCG@10 since the beginning; nothing
implemented them. Without a measured baseline, a change to chunking or indexing
cannot be shown to have helped — and both have now changed.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from research_engine import _rust as _rust_backend

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from uuid import UUID


def recall_at_k(retrieved: Sequence[UUID], relevant: Iterable[UUID], k: int) -> float:
    """Fraction of relevant passages that appear in the top *k*.

    Undefined with no relevant passages; returns 1.0, since nothing was missed.
    """
    rs = _rust_backend.rust_ret()
    if rs is not None:
        return rs.recall_at_k([str(p) for p in retrieved], [str(p) for p in relevant], k)

    relevant_set = set(relevant)
    if not relevant_set:
        return 1.0
    hits = len(set(retrieved[:k]) & relevant_set)
    return hits / len(relevant_set)


def precision_at_k(retrieved: Sequence[UUID], relevant: Iterable[UUID], k: int) -> float:
    rs = _rust_backend.rust_ret()
    if rs is not None:
        return rs.precision_at_k([str(p) for p in retrieved], [str(p) for p in relevant], k)

    if k <= 0:
        return 0.0
    relevant_set = set(relevant)
    return len(set(retrieved[:k]) & relevant_set) / k


def reciprocal_rank(retrieved: Sequence[UUID], relevant: Iterable[UUID]) -> float:
    """1/rank of the first relevant result; 0 if none is retrieved.

    Averaged over queries this is MRR. It answers "how far must the researcher
    read before finding something useful", which recall does not.
    """
    rs = _rust_backend.rust_ret()
    if rs is not None:
        return rs.reciprocal_rank([str(p) for p in retrieved], [str(p) for p in relevant])

    relevant_set = set(relevant)
    for position, passage_id in enumerate(retrieved, start=1):
        if passage_id in relevant_set:
            return 1.0 / position
    return 0.0


def dcg(gains: Sequence[float]) -> float:
    rs = _rust_backend.rust_ret()
    if rs is not None:
        return rs.dcg(list(gains))
    return sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))


def ndcg_at_k(
    retrieved: Sequence[UUID],
    relevant: Iterable[UUID] | dict[UUID, float],
    k: int,
) -> float:
    """Normalised discounted cumulative gain.

    *relevant* may be a set (binary relevance) or a mapping of passage id to a
    graded score, which is what a researcher's own judgments look like: some
    passages are on point, others merely adjacent.
    """
    rs = _rust_backend.rust_ret()
    if rs is not None:
        if isinstance(relevant, dict):
            rel = {str(pid): float(grade) for pid, grade in relevant.items()}
        else:
            rel = [str(pid) for pid in relevant]
        return rs.ndcg_at_k([str(p) for p in retrieved], rel, k)

    grades: dict[UUID, float] = (
        dict(relevant) if isinstance(relevant, dict) else {pid: 1.0 for pid in relevant}
    )
    if not grades:
        return 1.0

    actual = dcg([grades.get(pid, 0.0) for pid in retrieved[:k]])
    ideal = dcg(sorted(grades.values(), reverse=True)[:k])
    return actual / ideal if ideal else 0.0
