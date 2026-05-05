"""Tests for IngestionOrchestrator helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from research_engine.domain.documents import Document, DocumentFilter
from research_engine.services.ingestion.orchestrator import IngestionOrchestrator


class _FakeDocumentRepo:
    """In-memory DocumentRepo just for find_existing tests."""

    def __init__(self, docs: list[Document]) -> None:
        self._docs = docs

    async def iter_by_filter(self, filt: DocumentFilter):
        # Mirror the postgres repo's source_pattern semantics: case-insensitive
        # substring match. No metadata filtering (postgres impl ignores it too).
        for doc in self._docs:
            if filt.source_pattern and filt.source_pattern.lower() not in doc.source.lower():
                continue
            yield doc


class _FakePassageRepo:
    def __init__(self, counts: dict[UUID, int]) -> None:
        self._counts = counts

    async def get_by_document(self, document_id: UUID):
        # Return list of length N — find_existing only uses len().
        return [object()] * self._counts.get(document_id, 0)


def _doc(source: str, *, doc_id: UUID | None = None, title: str = "Doc") -> Document:
    return Document(
        id=doc_id or uuid4(),
        title=title,
        document_type="kindle_book",
        language=None,
        source=source,
        content_hash=b"\x00" * 32,
        parser="plugin_direct",
        parser_version="1.0",
        ingested_at=datetime.now(UTC),
        metadata={},
    )


def _orchestrator(docs: list[Document], passage_counts: dict[UUID, int]) -> IngestionOrchestrator:
    return IngestionOrchestrator(
        docs=_FakeDocumentRepo(docs),
        passages=_FakePassageRepo(passage_counts),
        embedding=object(),
        ingestion_runs=object(),
        dispatcher=object(),
        engine=object(),
    )


class TestFindExisting:
    @pytest.mark.asyncio
    async def test_exact_source_match(self):
        d = _doc("/tmp/extracted/B003NX6Z3W.txt")
        orch = _orchestrator([d], {d.id: 301})
        result = await orch.find_existing(source="/tmp/extracted/B003NX6Z3W.txt")
        assert len(result) == 1
        assert result[0]["document_id"] == str(d.id)
        assert result[0]["passage_count"] == 301

    @pytest.mark.asyncio
    async def test_exact_source_rejects_substring_hit(self):
        # Two docs whose sources both contain the ASIN, but only one matches exactly.
        d1 = _doc("/tmp/extracted/B003NX6Z3W.txt")
        d2 = _doc("/tmp/extracted/B003NX6Z3W.bak.txt")
        orch = _orchestrator([d1, d2], {d1.id: 10, d2.id: 20})
        result = await orch.find_existing(source="/tmp/extracted/B003NX6Z3W.txt")
        assert len(result) == 1
        assert result[0]["document_id"] == str(d1.id)

    @pytest.mark.asyncio
    async def test_source_pattern_substring_match(self):
        d1 = _doc("/tmp/extracted/B003NX6Z3W.txt")
        d2 = _doc("/tmp/extracted/B0090NUP8K.txt")
        orch = _orchestrator([d1, d2], {d1.id: 10, d2.id: 20})
        result = await orch.find_existing(source_pattern="B003NX6Z3W")
        assert len(result) == 1
        assert result[0]["document_id"] == str(d1.id)

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self):
        d = _doc("/tmp/extracted/B003NX6Z3W.txt")
        orch = _orchestrator([d], {d.id: 301})
        result = await orch.find_existing(source_pattern="BFAKEFAKE0")
        assert result == []

    @pytest.mark.asyncio
    async def test_requires_source_or_pattern(self):
        orch = _orchestrator([], {})
        with pytest.raises(ValueError):
            await orch.find_existing()
