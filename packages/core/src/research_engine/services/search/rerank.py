"""Reranking orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from research_engine.ports.reranker import RerankerPort


async def rerank_passages(
    reranker: RerankerPort,
    query: str,
    passage_ids: list[UUID],
    texts: list[str],
    k: int,
) -> list[tuple[UUID, float]]:
    """Rerank passage candidates using the configured reranker."""
    return await reranker.rerank(query, passage_ids, texts, k)
