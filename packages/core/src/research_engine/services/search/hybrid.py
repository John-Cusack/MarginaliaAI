"""Hybrid search service with filter pushdown and parallel retrieval."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from research_engine.domain.common import FusionMode
from research_engine.domain.errors import RerankUnavailable
from research_engine.domain.passages import (
    PassageHit,
    ScoreBreakdown,
    SearchQuery,
    SearchResult,
)
from research_engine.services.search.fusion import rrf_fuse, weighted_fuse
from research_engine.services.search.langconfig import pg_config

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from research_engine.domain.filter_extension import FilterExtension
    from research_engine.domain.passages import Passage
    from research_engine.ports.embedding import EmbeddingPort
    from research_engine.ports.repositories import PassageRepo
    from research_engine.ports.reranker import RerankerPort
    from research_engine.services.search.windows import PassageWindowReader

logger = structlog.get_logger()


class HybridSearchService:
    def __init__(
        self,
        passages: PassageRepo,
        embedding: EmbeddingPort,
        reranker: RerankerPort,
        get_filter_extensions: Callable[[], dict[str, FilterExtension]] | None = None,
        windows: PassageWindowReader | None = None,
    ) -> None:
        self._passages = passages
        self._embedding = embedding
        self._reranker = reranker
        self._get_filter_extensions = get_filter_extensions
        # Optional so a service can be constructed without the document-text and
        # node repositories — tests do, and a corpus with no canonical text has
        # nothing to widen into. The composition root always supplies one, so
        # expansion is on in practice rather than opt-in.
        self._windows = windows

    async def find_passages(self, query: SearchQuery) -> SearchResult:
        # Stage 1: Build candidate set from filters
        candidate_ids: list[UUID] | None = None
        filters_applied: dict = {}

        # An explicit language restricts keyword search to that stemmer; without
        # one, keyword_search spans every language present in the corpus. Never
        # assume English — the corpus is multilingual and bge-m3 is too.
        lang_config = (
            pg_config(query.filters.language)
            if query.filters and query.filters.language
            else None
        )

        if query.filters:
            filter_dict = query.filters.model_dump(exclude_none=True)
            # extension_logic has a non-None default, so it is present even when
            # nothing is actually being filtered. On its own it narrows nothing;
            # treating it as a filter would run a full-corpus candidate scan and
            # report a filter that did no work.
            if filter_dict.keys() - {"extension_logic"}:
                exts = self._get_filter_extensions() if self._get_filter_extensions else None
                candidate_ids = await self._passages.filter_candidate_ids(
                    filter_dict,
                    filter_extensions=exts,
                )
                filters_applied = filter_dict
                if not candidate_ids:
                    return SearchResult(hits=[], total_candidates=0, applied_filters=filters_applied)

        total_candidates = len(candidate_ids) if candidate_ids else 0

        # Handle single-mode searches
        if query.fusion_mode == FusionMode.vector_only:
            query_vec = await self._embedding.embed(query.text)
            vec_hits = await self._passages.vector_search(
                query_vec, self._embedding.model_name, self._embedding.model_version,
                candidate_ids, query.k if not query.rerank else query.rerank_n,
            )
            fused = [(pid, s, {"vector": s}) for pid, s in vec_hits]
        elif query.fusion_mode == FusionMode.keyword_only:
            kw_hits = await self._passages.keyword_search(
                query.text, lang_config, candidate_ids,
                query.k if not query.rerank else query.rerank_n,
            )
            fused = [(pid, s, {"keyword": s}) for pid, s in kw_hits]
        else:
            # Stage 2: Parallel vector + keyword retrieval
            query_vec_task = asyncio.create_task(self._embedding.embed(query.text))
            query_vec = await query_vec_task

            vec_task = asyncio.create_task(
                self._passages.vector_search(
                    query_vec, self._embedding.model_name, self._embedding.model_version,
                    candidate_ids, query.k_vec,
                )
            )
            kw_task = asyncio.create_task(
                self._passages.keyword_search(
                    query.text, lang_config, candidate_ids, query.k_kw,
                )
            )
            vec_hits, kw_hits = await asyncio.gather(vec_task, kw_task)

            # Stage 3: Fusion
            if query.fusion_mode == FusionMode.weighted:
                fused = weighted_fuse(vec_hits, kw_hits, alpha=query.alpha)
            else:
                fused = rrf_fuse(vec_hits, kw_hits)

        # Stage 4: Optional rerank
        top_n = fused[:query.rerank_n] if query.rerank else fused[:query.k]
        degraded: list[str] = []
        final_items = top_n[:query.k]

        # One read for the whole candidate set, reused by both the reranker and
        # hydration. What the reranker receives is chunk text and only chunk
        # text: the expanded window is a read-side concern, and letting it reach
        # the cross-encoder would make ranking depend on it.
        top_ids = [pid for pid, _, _ in top_n]
        loaded = {p.id: p for p in await self._passages.get_many(top_ids)}

        if query.rerank and top_n:
            top_texts = [
                loaded[pid].text if pid in loaded else "" for pid in top_ids
            ]
            try:
                reranked = await self._reranker.rerank(
                    query.text, top_ids, top_texts, query.k,
                )
            except RerankUnavailable as exc:
                # Return the fused ranking rather than nothing. Reranking
                # refines an ordering that is already useful, so an unreachable
                # cross-encoder should cost precision, not the whole answer —
                # and `degraded` means the caller can tell the difference.
                logger.warning(
                    "rerank_skipped",
                    error=str(exc),
                    candidates=len(top_n),
                    detail="Returning fused results unreranked.",
                )
                degraded.append("rerank_unavailable")
            else:
                # Merge rerank scores with existing breakdowns
                breakdown_map = {pid: bd for pid, _, bd in top_n}
                final_items = []
                for pid, rerank_score in reranked:
                    bd = breakdown_map.get(pid, {})
                    bd["rerank"] = rerank_score
                    final_items.append((pid, rerank_score, bd))

        # Stage 5: Hydrate
        hits = await self._hydrate(final_items, loaded)

        return SearchResult(
            hits=hits,
            total_candidates=total_candidates,
            applied_filters=filters_applied,
            degraded=degraded,
        )

    async def similar_to(
        self, passage_id: UUID, k: int = 20, candidate_ids: list[UUID] | None = None
    ) -> list[PassageHit]:
        """Find passages similar to a given passage."""
        embedding = await self._passages.get_embedding(
            passage_id, self._embedding.model_name, self._embedding.model_version,
        )
        if not embedding:
            return []

        hits = await self._passages.vector_search(
            embedding, self._embedding.model_name, self._embedding.model_version,
            candidate_ids, k + 1,  # +1 to exclude self
        )
        # Exclude self
        hits = [(pid, s) for pid, s in hits if pid != passage_id][:k]
        return await self._hydrate([(pid, s, {"vector": s}) for pid, s in hits])

    async def _hydrate(
        self,
        items: list[tuple[UUID, float, dict]],
        loaded: dict[UUID, Passage] | None = None,
    ) -> list[PassageHit]:
        """Turn ranked ids into hits.

        *loaded* is the candidate set `find_passages` already read. `similar_to`
        has no such set and passes nothing, so the fetch happens here instead —
        one query either way, never one per hit.
        """
        if loaded is None:
            loaded = {
                p.id: p for p in await self._passages.get_many([i[0] for i in items])
            }
        found = [(i, loaded[i[0]]) for i in items if i[0] in loaded]
        # After ranking, never before: the window is what a reader reads, and
        # letting it reach the cross-encoder would make scores depend on it.
        windows = (
            await self._windows.read([p for _, p in found]) if self._windows else {}
        )

        hits = []
        for (pid, score, breakdown), passage in found:
            hits.append(
                PassageHit(
                    passage_id=pid,
                    document_id=passage.document_id,
                    score=score,
                    score_breakdown=ScoreBreakdown(
                        vector=breakdown.get("vector") or breakdown.get("list_0", {}).get("score"),
                        keyword=breakdown.get("keyword") or breakdown.get("list_1", {}).get("score"),
                        rerank=breakdown.get("rerank"),
                        rrf=score if "list_0" in breakdown else None,
                    ),
                    text=passage.text,
                    metadata=passage.metadata,
                    locator=passage.locator,
                    char_start=passage.char_start,
                    char_end=passage.char_end,
                    node_id=passage.node_id,
                    window=windows.get(pid),
                )
            )
        return hits
