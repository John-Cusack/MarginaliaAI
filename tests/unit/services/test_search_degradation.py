"""A search whose reranker is unreachable answers anyway, and says it did.

The alternative behaviours are both worse. Raising loses an answer the engine
can still give from fused rankings in 291 ms. Falling back to a CPU
cross-encoder costs 48.8 s of a 49.1 s search, measured, with no indication of
why. Returning fused results and flagging them degrades in quality, visibly,
rather than in latency, invisibly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from research_engine.domain.errors import RerankUnavailable
from research_engine.domain.passages import SearchQuery
from research_engine.services.search.hybrid import HybridSearchService

IDS = [uuid.uuid4() for _ in range(4)]
DOC = uuid.uuid4()


class FakePassages:
    async def vector_search(self, vec, model, version, candidates, k):
        return [(pid, 1.0 - i * 0.1) for i, pid in enumerate(IDS[:k])]

    async def keyword_search(self, text, lang, candidates, k):
        return [(pid, 0.5) for pid in IDS[:k]]

    async def get(self, pid):
        return SimpleNamespace(
            document_id=DOC, text=f"passage {pid}", metadata={}, locator={}
        )


class FakeEmbedding:
    model_name, model_version, dim = "BAAI/bge-m3", "1.0", 1024

    async def embed(self, text):
        return [0.1] * self.dim


class DeadReranker:
    async def rerank(self, query, passage_ids, texts, k):
        raise RerankUnavailable("gpu-host is asleep")


class ReversingReranker:
    async def rerank(self, query, passage_ids, texts, k):
        return [(pid, float(i)) for i, pid in enumerate(reversed(passage_ids))][:k]


def _service(reranker):
    return HybridSearchService(
        passages=FakePassages(), embedding=FakeEmbedding(), reranker=reranker
    )


@pytest.mark.asyncio
async def test_unreachable_reranker_still_returns_results():
    result = await _service(DeadReranker()).find_passages(
        SearchQuery(text="mishpat", k=3, rerank=True)
    )

    assert len(result.hits) == 3
    assert result.degraded == ["rerank_unavailable"]


@pytest.mark.asyncio
async def test_degraded_results_keep_fusion_order():
    """Falling back to the RRF ranking is the point — not an arbitrary order."""
    fused = await _service(DeadReranker()).find_passages(
        SearchQuery(text="mishpat", k=3, rerank=False)
    )
    degraded = await _service(DeadReranker()).find_passages(
        SearchQuery(text="mishpat", k=3, rerank=True)
    )

    assert [h.passage_id for h in degraded.hits] == [h.passage_id for h in fused.hits]


@pytest.mark.asyncio
async def test_a_working_reranker_leaves_no_degradation_marker():
    result = await _service(ReversingReranker()).find_passages(
        SearchQuery(text="mishpat", k=3, rerank=True)
    )

    assert result.degraded == []
    assert result.hits[0].score_breakdown.rerank is not None


@pytest.mark.asyncio
async def test_reranking_actually_reorders_when_it_works():
    """Guards the degradation path from passing vacuously: if rerank never
    changed the order, the two branches would be indistinguishable."""
    working = await _service(ReversingReranker()).find_passages(
        SearchQuery(text="mishpat", k=3, rerank=True)
    )
    degraded = await _service(DeadReranker()).find_passages(
        SearchQuery(text="mishpat", k=3, rerank=True)
    )

    assert [h.passage_id for h in working.hits] != [
        h.passage_id for h in degraded.hits
    ]
