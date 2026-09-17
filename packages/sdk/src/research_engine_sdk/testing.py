"""Portable mocks and behavior contracts for plugin unit tests."""

from __future__ import annotations

import re
from typing import Any

from research_engine_sdk.chunking import ABSOLUTE_MAX_TOKENS
from research_engine_sdk.types import SearchQuery, SearchResult

OVERSHOOT_TOLERANCE = 1.5
MAX_AMPLIFICATION = 2.0
_SEAM = re.compile(r"\s+|[。！？；、，]")

CONTRACT_TEXTS: dict[str, str] = {
    "empty": "",
    "whitespace_only": "   \n\n\t  ",
    "single_sentence": "One sentence with no terminator",
    "prose": (
        "The archive holds letters. Each letter has a date. "
        "Some dates are approximate. Others are exact. "
    )
    * 40,
    "index": "Index\n\n" + "".join(f"entry {number}\n" for number in range(2400)),
    "no_boundaries": "word " * 2000,
    "cjk": "第一句話寫在這裡。第二句話也寫在這裡，內容比較長一些。" * 900,
    "greek": (
        "λόγος, ου, ὁ πρὸς τὸν θεόν, καὶ θεὸς ἦν ὁ λόγος· "
        "οὗτος ἦν ἐν ἀρχῇ πρὸς τὸν θεόν. "
    )
    * 600,
    "crlf": "First line.\r\nSecond line.\r\n\r\nThird.",
}


def _independent_token_estimate(text: str) -> int:
    if not text:
        return 1
    dense = sum(1 for char in text if ord(char) >= 128)
    return max(1, int((len(text) - dense) / 4.0 + dense / 1.5))


async def call_chunker(chunker: Any, text: str) -> list[Any]:
    if getattr(chunker, "consumes", "text") == "sections":
        if not text.strip():
            return []
        start, end = 0, len(text)
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        return await chunker.chunk(
            [{"char_start": start, "char_end": end, "level": 1}],
            None,
            full_text=text,
        )
    return await chunker.chunk(text)


async def assert_chunker_contract(
    chunker: Any,
    *,
    texts: dict[str, str] | None = None,
    absolute_max_tokens: int = ABSOLUTE_MAX_TOKENS,
) -> None:
    """Assert deterministic, gap-free, bounded, span-faithful chunk output."""

    label = getattr(chunker, "id", type(chunker).__name__)
    assert hasattr(chunker, "max_passage_tokens"), (
        f"{label} must declare max_passage_tokens (use None for deliberately unbounded)"
    )
    limit = chunker.max_passage_tokens
    for name, text in (texts or CONTRACT_TEXTS).items():
        drafts = await call_chunker(chunker, text)
        if not text.strip():
            assert drafts == [], f"{label}: emitted passages for empty input ({name})"
            continue
        assert drafts, f"{label}: emitted nothing for {name}"
        assert [draft.position for draft in drafts] == list(range(len(drafts)))
        assert [draft.char_start for draft in drafts] == sorted(
            draft.char_start for draft in drafts
        )
        assert len({(draft.char_start, draft.char_end) for draft in drafts}) == len(drafts)

        cursor = 0
        for draft in drafts:
            assert 0 <= draft.char_start <= draft.char_end <= len(text)
            assert draft.text == text[draft.char_start : draft.char_end]
            assert draft.text.strip()
            if draft.char_start > cursor:
                assert not text[cursor : draft.char_start].strip(), (
                    f"{label}: dropped content on {name}"
                )
            cursor = max(cursor, draft.char_end)
            tokens = _independent_token_estimate(draft.text)
            assert tokens <= absolute_max_tokens
            if limit is not None and len(_SEAM.findall(draft.text)) + 1 > 1:
                assert tokens <= limit * OVERSHOOT_TOLERANCE
        assert not text[cursor:].strip(), f"{label}: dropped tail on {name}"
        emitted = sum(len(draft.text) for draft in drafts)
        assert emitted / len(text.strip()) <= MAX_AMPLIFICATION

        again = await call_chunker(chunker, text)
        assert [(draft.char_start, draft.char_end) for draft in again] == [
            (draft.char_start, draft.char_end) for draft in drafts
        ], f"{label}: chunking is not deterministic on {name}"


class MockCorpusClient:
    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self._docs = {str(document["id"]): document for document in documents or []}

    async def find_passages(
        self, query: str, filters: dict[str, Any] | None = None, k: int = 20
    ) -> SearchResult:
        return SearchResult(hits=[], total_candidates=0)

    async def find_passages_advanced(self, query: SearchQuery) -> SearchResult:
        return SearchResult(hits=[], total_candidates=0)

    async def get_document(self, document_id: Any) -> dict[str, Any] | None:
        return self._docs.get(str(document_id))

    async def get_document_outline(
        self, document_id: Any, dated_only: bool = False
    ) -> list[dict[str, Any]]:
        return []

    async def get_passage_context(
        self, passage_id: Any, before: int = 0, after: int = 0
    ) -> dict[str, Any]:
        return {"target": None, "before": [], "after": []}


class MockLLMClient:
    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or ["Mock response"])
        self._call_count = 0

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        index = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return self._responses[index]

    async def structured(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {"records": []}


__all__ = [
    "ABSOLUTE_MAX_TOKENS",
    "CONTRACT_TEXTS",
    "MAX_AMPLIFICATION",
    "OVERSHOOT_TOLERANCE",
    "MockCorpusClient",
    "MockLLMClient",
    "assert_chunker_contract",
    "call_chunker",
]
