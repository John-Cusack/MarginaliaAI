"""Embedding port interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingPort(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    @property
    def dim(self) -> int: ...

    async def embed(self, text: str) -> list[float]:
        """Embed a single text."""
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts."""
        ...
