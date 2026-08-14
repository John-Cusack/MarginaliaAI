"""Retrieval metrics.

Hand-computed expectations throughout: a metric implementation checked against
itself proves nothing.
"""

from __future__ import annotations

import math
from uuid import UUID

import pytest

from research_engine.eval.metrics import (
    dcg,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

pytestmark = pytest.mark.unit


def pid(n: int) -> UUID:
    return UUID(int=n)


A, B, C, D, E = (pid(i) for i in range(1, 6))


class TestRecall:
    def test_all_relevant_retrieved(self) -> None:
        assert recall_at_k([A, B, C], {A, B}, 3) == 1.0

    def test_half_retrieved(self) -> None:
        assert recall_at_k([A, D, E], {A, B}, 3) == 0.5

    def test_cutoff_excludes_later_hits(self) -> None:
        """A relevant result at rank 3 does not count towards recall@2."""
        assert recall_at_k([D, E, A], {A}, 2) == 0.0
        assert recall_at_k([D, E, A], {A}, 3) == 1.0

    def test_no_relevant_passages_is_not_a_failure(self) -> None:
        assert recall_at_k([A, B], set(), 2) == 1.0

    def test_empty_retrieval(self) -> None:
        assert recall_at_k([], {A}, 10) == 0.0


class TestPrecision:
    def test_counts_against_k_not_result_length(self) -> None:
        """Two hits in a top-10 is 0.2, even if only two results came back."""
        assert precision_at_k([A, B], {A, B}, 10) == pytest.approx(0.2)

    def test_full_precision(self) -> None:
        assert precision_at_k([A, B], {A, B}, 2) == 1.0


class TestReciprocalRank:
    def test_first_position(self) -> None:
        assert reciprocal_rank([A, B], {A}) == 1.0

    def test_third_position(self) -> None:
        assert reciprocal_rank([D, E, A], {A}) == pytest.approx(1 / 3)

    def test_uses_the_first_relevant_only(self) -> None:
        assert reciprocal_rank([D, A, B], {A, B}) == 0.5

    def test_nothing_relevant(self) -> None:
        assert reciprocal_rank([D, E], {A}) == 0.0


class TestNDCG:
    def test_dcg_matches_the_definition(self) -> None:
        # 1/log2(2) + 1/log2(3) = 1 + 0.6309…
        assert dcg([1.0, 1.0]) == pytest.approx(1 + 1 / math.log2(3))

    def test_perfect_ranking_scores_one(self) -> None:
        assert ndcg_at_k([A, B, C], {A, B, C}, 3) == pytest.approx(1.0)

    def test_order_matters(self) -> None:
        good = ndcg_at_k([A, D, E], {A}, 3)
        bad = ndcg_at_k([D, E, A], {A}, 3)
        assert good > bad
        assert good == pytest.approx(1.0)
        assert bad == pytest.approx(1 / math.log2(4))

    def test_graded_relevance_rewards_putting_the_best_first(self) -> None:
        grades = {A: 3.0, B: 1.0}
        assert ndcg_at_k([A, B], grades, 2) > ndcg_at_k([B, A], grades, 2)

    def test_graded_perfect_order_scores_one(self) -> None:
        assert ndcg_at_k([A, B], {A: 3.0, B: 1.0}, 2) == pytest.approx(1.0)

    def test_irrelevant_results_contribute_nothing(self) -> None:
        assert ndcg_at_k([D, E], {A: 1.0}, 2) == 0.0

    def test_no_judgments_is_not_a_failure(self) -> None:
        assert ndcg_at_k([A], {}, 5) == 1.0
