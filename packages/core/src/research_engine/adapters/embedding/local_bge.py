"""Local BGE-M3 embedding adapter using sentence-transformers."""

from __future__ import annotations

import asyncio
from functools import partial

from sentence_transformers import SentenceTransformer


class LocalBGEEmbedding:
    def __init__(self, model_name: str = "BAAI/bge-m3", dim: int = 1024) -> None:
        self._model_name = model_name
        self._dim = dim
        self._model: SentenceTransformer | None = None

    def _ensure_model(self) -> SentenceTransformer:
        if self._model is None:
            self._model = SentenceTransformer(self._model_name)
        return self._model

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_version(self) -> str:
        return "1.0"

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        loop = asyncio.get_event_loop()
        model = self._ensure_model()
        embedding = await loop.run_in_executor(
            None, partial(model.encode, text, normalize_embeddings=True)
        )
        return embedding.tolist()[:self._dim]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        loop = asyncio.get_event_loop()
        model = self._ensure_model()
        embeddings = await loop.run_in_executor(
            None, partial(model.encode, texts, normalize_embeddings=True, batch_size=32)
        )
        return [e.tolist()[:self._dim] for e in embeddings]
