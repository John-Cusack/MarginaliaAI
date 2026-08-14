"""No-op reranker passthrough."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID


class NoopReranker:
    """Passthrough reranker that returns inputs unchanged."""

    async def rerank(
        self, query: str, passage_ids: list[UUID], texts: list[str], k: int
    ) -> list[tuple[UUID, float]]:
        return [(pid, 1.0 - i * 0.01) for i, pid in enumerate(passage_ids[:k])]
