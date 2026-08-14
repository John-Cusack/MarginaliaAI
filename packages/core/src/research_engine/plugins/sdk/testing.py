"""Testing utilities for plugin authors."""

from __future__ import annotations

from typing import Any


class MockCorpusClient:
    """Mock corpus client for plugin testing."""

    def __init__(self, documents: list[dict] | None = None) -> None:
        self._docs = {d["id"]: d for d in (documents or [])}

    async def find_passages(self, query: str, filters: dict | None = None, k: int = 20) -> list:
        return []

    async def get_document(self, document_id: str) -> dict | None:
        return self._docs.get(document_id)

    async def get_passage_context(self, passage_id: str, before: int = 0, after: int = 0) -> dict:
        return {"target": None, "before": [], "after": []}


class MockLLMClient:
    """Mock LLM client that returns configured responses."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or ["Mock response"])
        self._call_count = 0

    async def complete(self, messages: list[dict], **kwargs: Any) -> str:
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return self._responses[idx]

    async def structured(self, messages: list[dict], schema: dict, **kwargs: Any) -> dict:
        return {"records": []}
