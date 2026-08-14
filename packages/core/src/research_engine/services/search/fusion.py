"""Search result fusion algorithms."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

RRF_K = 60


def rrf_fuse(
    *ranked_lists: list[tuple[UUID, float]],
    k: int = 60,
) -> list[tuple[UUID, float, dict]]:
    """Reciprocal Rank Fusion across multiple ranked lists.

    Returns [(passage_id, rrf_score, breakdown)] sorted by score descending.
    """
    scores: dict[UUID, float] = defaultdict(float)
    breakdowns: dict[UUID, dict] = defaultdict(dict)

    for list_idx, hits in enumerate(ranked_lists):
        for rank, (pid, score) in enumerate(hits):
            scores[pid] += 1.0 / (RRF_K + rank + 1)
            breakdowns[pid][f"list_{list_idx}"] = {
                "rank": rank + 1,
                "score": score,
            }

    sorted_results = sorted(scores.items(), key=lambda x: -x[1])
    return [(pid, score, breakdowns[pid]) for pid, score in sorted_results]


def weighted_fuse(
    vec_hits: list[tuple[UUID, float]],
    kw_hits: list[tuple[UUID, float]],
    alpha: float = 0.5,
) -> list[tuple[UUID, float, dict]]:
    """Weighted sum fusion with min-max normalization.

    alpha controls weight of vector score (1-alpha for keyword).
    """
    def normalize(hits: list[tuple[UUID, float]]) -> dict[UUID, float]:
        if not hits:
            return {}
        scores = [s for _, s in hits]
        min_s, max_s = min(scores), max(scores)
        rng = max_s - min_s if max_s > min_s else 1.0
        return {pid: (s - min_s) / rng for pid, s in hits}

    vec_norm = normalize(vec_hits)
    kw_norm = normalize(kw_hits)
    all_ids = set(vec_norm) | set(kw_norm)

    results = []
    for pid in all_ids:
        vs = vec_norm.get(pid, 0.0)
        ks = kw_norm.get(pid, 0.0)
        combined = alpha * vs + (1 - alpha) * ks
        breakdown = {"vector_norm": vs, "keyword_norm": ks}
        results.append((pid, combined, breakdown))

    results.sort(key=lambda x: -x[1])
    return results
