"""Reranker port interface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from uuid import UUID


@runtime_checkable
class RerankerPort(Protocol):
    async def rerank(
        self, query: str, passage_ids: list[UUID], texts: list[str], k: int
    ) -> list[tuple[UUID, float]]:
        """Rerank passages by relevance to query. Returns [(passage_id, score)]."""
        ...
