"""Run a query set against one or more engine configurations.

The runner takes a *container factory*, not a container. Fusion mode, alpha and
rerank arrive per-query on `SearchQuery`, but embedding model, reranker and
chunker are constructor-injected in `composition.py` — so comparing them means
standing up alternate wirings, not passing different arguments.
"""

from __future__ import annotations

import statistics
from collections.abc import Awaitable, Callable  # noqa: TC003 - runtime type alias below
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from research_engine.domain.passages import SearchFilters, SearchQuery
from research_engine.eval.metrics import (
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from research_engine.eval.queryset import EvalQuery, QuerySet

ContainerFactory = Callable[[], Awaitable[Any]]


@dataclass
class QueryResult:
    query: str
    retrieved: list[Any]
    recall: float
    precision: float
    reciprocal_rank: float
    ndcg: float
    #: Retrieval returned nothing at all — usually a filter that eliminated
    #: every candidate, which averages look identical to "ranked badly".
    empty: bool = False


@dataclass
class RunResult:
    config: str
    k: int
    queries: list[QueryResult] = field(default_factory=list)

    def _mean(self, attr: str) -> float:
        values = [getattr(q, attr) for q in self.queries]
        return statistics.mean(values) if values else 0.0

    @property
    def recall(self) -> float:
        return self._mean("recall")

    @property
    def precision(self) -> float:
        return self._mean("precision")

    @property
    def mrr(self) -> float:
        return self._mean("reciprocal_rank")

    @property
    def ndcg(self) -> float:
        return self._mean("ndcg")

    @property
    def empty_queries(self) -> int:
        return sum(1 for q in self.queries if q.empty)

    def summary(self) -> dict[str, float | int | str]:
        return {
            "config": self.config,
            "queries": len(self.queries),
            f"recall@{self.k}": round(self.recall, 4),
            f"precision@{self.k}": round(self.precision, 4),
            "mrr": round(self.mrr, 4),
            f"ndcg@{self.k}": round(self.ndcg, 4),
            "empty": self.empty_queries,
        }


async def run_queryset(
    factory: ContainerFactory,
    queryset: QuerySet,
    *,
    k: int = 10,
    config_name: str = "default",
    search_overrides: dict[str, Any] | None = None,
) -> RunResult:
    """Evaluate *queryset* against the engine that *factory* builds."""
    container = await factory()
    result = RunResult(config=config_name, k=k)
    try:
        for judged in queryset.queries:
            retrieved = await _search(container, judged, k, search_overrides or {})
            result.queries.append(
                QueryResult(
                    query=judged.query,
                    retrieved=retrieved,
                    recall=recall_at_k(retrieved, judged.relevant_ids, k),
                    precision=precision_at_k(retrieved, judged.relevant_ids, k),
                    reciprocal_rank=reciprocal_rank(retrieved, judged.relevant_ids),
                    ndcg=ndcg_at_k(retrieved, judged.relevant, k),
                    empty=not retrieved,
                )
            )
    finally:
        await container.close()
    return result


async def _search(
    container: Any, judged: EvalQuery, k: int, overrides: dict[str, Any]
) -> list[Any]:
    query = SearchQuery(
        text=judged.query,
        k=k,
        filters=SearchFilters(**judged.filters) if judged.filters else None,
        **overrides,
    )
    result = await container.search.find_passages(query)
    return [hit.passage_id for hit in result.hits]


def compare(runs: Sequence[RunResult]) -> list[dict[str, Any]]:
    """Paired per-metric diff against the first run.

    Absolute numbers on a hand-built query set mean little; the movement between
    two configurations is the signal.
    """
    if not runs:
        return []
    baseline = runs[0]
    rows: list[dict[str, Any]] = [baseline.summary()]
    for run in runs[1:]:
        row = run.summary()
        for metric, before, after in (
            (f"recall@{run.k}", baseline.recall, run.recall),
            (f"precision@{run.k}", baseline.precision, run.precision),
            ("mrr", baseline.mrr, run.mrr),
            (f"ndcg@{run.k}", baseline.ndcg, run.ndcg),
        ):
            row[f"Δ {metric}"] = round(after - before, 4)
        rows.append(row)
    return rows
