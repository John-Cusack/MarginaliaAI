"""Retrieval evaluation: metrics, query sets, and A/B runner."""

from research_engine.eval.metrics import (
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from research_engine.eval.queryset import EvalQuery, QuerySet
from research_engine.eval.runner import RunResult, compare, run_queryset

__all__ = [
    "EvalQuery",
    "QuerySet",
    "RunResult",
    "compare",
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
    "run_queryset",
]
