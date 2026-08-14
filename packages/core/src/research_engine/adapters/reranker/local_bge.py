"""Local BGE reranker using cross-encoder."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING

from sentence_transformers import CrossEncoder

if TYPE_CHECKING:
    from uuid import UUID


class LocalBGEReranker:
    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3") -> None:
        self._model_name = model_name
        self._model: CrossEncoder | None = None

    def _ensure_model(self) -> CrossEncoder:
        if self._model is None:
            self._model = CrossEncoder(self._model_name)
        return self._model

    async def rerank(
        self, query: str, passage_ids: list[UUID], texts: list[str], k: int
    ) -> list[tuple[UUID, float]]:
        if not texts:
            return []

        model = self._ensure_model()
        pairs = [[query, t] for t in texts]

        loop = asyncio.get_event_loop()
        scores = await loop.run_in_executor(None, partial(model.predict, pairs))

        scored = list(zip(passage_ids, [float(s) for s in scores], strict=False))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]
