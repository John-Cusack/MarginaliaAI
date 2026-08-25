"""Local BGE reranker using a cross-encoder.

Be aware of what this costs. Measured on this corpus with `rerank_n=30`, on a
host where `torch.cuda.is_available()` is False, a single reranked search takes
48.8 s against 291 ms unreranked — the cross-encoder is 99.4% of query latency.
On a working GPU it is closer to a second. Running this locally is therefore a
reasonable default only when the accelerator actually works; see
`adapters/inference/routing.py` for how that choice is made.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING

from sentence_transformers import CrossEncoder

from research_engine.adapters.reranker.scoring import rank_from_scores

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID


class LocalBGEReranker:
    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3") -> None:
        self._model_name = model_name
        self._model: CrossEncoder | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_version(self) -> str:
        return "1.0"

    def _ensure_model(self) -> CrossEncoder:
        if self._model is None:
            self._model = CrossEncoder(self._model_name)
        return self._model

    async def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """Relevance of each text to *query*, in input order."""
        if not texts:
            return []
        model = self._ensure_model()
        pairs = [[query, t] for t in texts]
        loop = asyncio.get_event_loop()
        scores = await loop.run_in_executor(None, partial(model.predict, pairs))
        return [float(s) for s in scores]

    async def rerank(
        self, query: str, passage_ids: list[UUID], texts: list[str], k: int
    ) -> list[tuple[UUID, float]]:
        if not texts:
            return []
        return rank_from_scores(passage_ids, await self.score(query, texts), k)
