"""Tests for CorpusServiceAdapter — the SDK CorpusClient implementation
that conforms HybridSearchService + DocumentRepo + PassageRepo to the
plugin-facing Protocol surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from research_engine.adapters.corpus_client import CorpusServiceAdapter
from research_engine.domain.documents import Document
from research_engine.domain.passages import (
    Passage,
    PassageHit,
    SearchQuery,
    SearchResult,
)
from research_engine.plugins.sdk.clients import CorpusClient

# ---------- Helpers ----------


def _doc(doc_id: UUID, *, title: str = "Doc", source: str = "/x.pdf") -> Document:
    return Document(
        id=doc_id,
        title=title,
        document_type="academic_journal",
        source=source,
        content_hash=b"\x00" * 32,
        parser="pdf_text",
        parser_version="1",
        ingested_at=datetime.now(UTC),
        metadata={},
    )


def _passage(p_id: UUID, doc_id: UUID, *, position: int = 0, text: str = "hello") -> Passage:
    return Passage(
        id=p_id,
        document_id=doc_id,
        position=position,
        text=text,
        chunker="prose_window",
        chunker_version="1",
        content_hash=b"\x00" * 32,
        created_at=datetime.now(UTC),
    )


def _hit(p_id: UUID, doc_id: UUID, *, score: float = 0.9) -> PassageHit:
    return PassageHit(
        passage_id=p_id,
        document_id=doc_id,
        score=score,
        text="hello",
    )


def _build_adapter(
    *, search_result: SearchResult | None = None,
    doc: Document | None = None,
    passages: list[Passage] | None = None,
    context: tuple[list[Passage], Passage, list[Passage]] | None = None,
) -> tuple[CorpusServiceAdapter, AsyncMock, AsyncMock, AsyncMock]:
    search = AsyncMock()
    search.find_passages = AsyncMock(return_value=search_result)
    documents = AsyncMock()
    documents.get = AsyncMock(return_value=doc)
    passages_repo = AsyncMock()
    passages_repo.get_by_document = AsyncMock(return_value=passages or [])
    passages_repo.get_context = AsyncMock(return_value=context)
    adapter = CorpusServiceAdapter(search, documents, passages_repo)
    return adapter, search, documents, passages_repo


# ---------- Protocol conformance ----------


class TestProtocol:
    def test_adapter_conforms_to_corpus_client(self) -> None:
        adapter, *_ = _build_adapter()
        # CorpusClient is a Protocol with @runtime_checkable not set;
        # conformance is structural, so check method presence explicitly.
        for name in ("find_passages", "find_passages_advanced",
                     "get_document", "get_passage_context"):
            assert callable(getattr(adapter, name))

    def test_protocol_imports_from_sdk(self) -> None:
        # Smoke check that the Protocol still imports from the SDK surface.
        assert CorpusClient.__name__ == "CorpusClient"


# ---------- find_passages ----------


class TestFindPassages:
    @pytest.mark.asyncio
    async def test_translates_simple_call_to_search_query(self) -> None:
        doc_id = uuid4()
        result = SearchResult(
            hits=[_hit(uuid4(), doc_id)], total_candidates=1
        )
        adapter, search, *_ = _build_adapter(search_result=result)

        out = await adapter.find_passages("query text", k=5)

        assert out is result
        search.find_passages.assert_awaited_once()
        sent: SearchQuery = search.find_passages.await_args.args[0]
        assert isinstance(sent, SearchQuery)
        assert sent.text == "query text"
        assert sent.k == 5
        assert sent.filters is None

    @pytest.mark.asyncio
    async def test_positional_args_supported(self) -> None:
        # find_passages(query, filters, k) must accept positional args so older
        # third-party plugins calling positionally don't break (no keyword-only).
        result = SearchResult(hits=[], total_candidates=0)
        adapter, search, *_ = _build_adapter(search_result=result)

        await adapter.find_passages("q", None, 7)

        sent: SearchQuery = search.find_passages.await_args.args[0]
        assert sent.k == 7
        assert sent.filters is None

    @pytest.mark.asyncio
    async def test_filters_dict_translates_to_search_filters(self) -> None:
        result = SearchResult(hits=[], total_candidates=0)
        adapter, search, *_ = _build_adapter(search_result=result)

        await adapter.find_passages(
            "q",
            filters={
                "document_types": ["academic_journal"],
                "extensions": {"academic_paper": {"year_min": 2000}},
            },
        )

        sent: SearchQuery = search.find_passages.await_args.args[0]
        assert sent.filters is not None
        assert sent.filters.document_types == ["academic_journal"]
        assert sent.filters.extensions == {"academic_paper": {"year_min": 2000}}

    @pytest.mark.asyncio
    async def test_advanced_passes_search_query_through(self) -> None:
        result = SearchResult(hits=[], total_candidates=0)
        adapter, search, *_ = _build_adapter(search_result=result)

        sq = SearchQuery(text="advanced", k=3, alpha=0.7, rerank=False)
        await adapter.find_passages_advanced(sq)

        search.find_passages.assert_awaited_once_with(sq)


# ---------- get_document ----------


class TestGetDocument:
    @pytest.mark.asyncio
    async def test_returns_dict_with_passages(self) -> None:
        doc_id = uuid4()
        p1, p2 = uuid4(), uuid4()
        doc = _doc(doc_id, title="Test Paper", source="https://doi.org/10.1/x")
        passages = [
            _passage(p2, doc_id, position=1, text="second"),
            _passage(p1, doc_id, position=0, text="first"),
        ]
        adapter, *_ = _build_adapter(doc=doc, passages=passages)

        out = await adapter.get_document(doc_id)

        assert out is not None
        assert out["id"] == str(doc_id)
        assert out["title"] == "Test Paper"
        assert out["source"] == "https://doi.org/10.1/x"
        # Passages must be sorted by position regardless of repo order.
        assert [p["position"] for p in out["passages"]] == [0, 1]
        assert [p["text"] for p in out["passages"]] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self) -> None:
        adapter, _search, _docs, passages_repo = _build_adapter(doc=None)
        out = await adapter.get_document(uuid4())
        assert out is None
        passages_repo.get_by_document.assert_not_awaited()


# ---------- get_passage_context ----------


class TestGetPassageContext:
    @pytest.mark.asyncio
    async def test_returns_target_with_neighbors(self) -> None:
        doc_id = uuid4()
        target = _passage(uuid4(), doc_id, position=2, text="target")
        before = [_passage(uuid4(), doc_id, position=1, text="before")]
        after = [_passage(uuid4(), doc_id, position=3, text="after")]
        adapter, *_ = _build_adapter(context=(before, target, after))

        out = await adapter.get_passage_context(target.id, before=1, after=1)

        assert out["target"]["text"] == "target"
        assert out["before"][0]["text"] == "before"
        assert out["after"][0]["text"] == "after"
        assert out["document_id"] == str(doc_id)

    @pytest.mark.asyncio
    async def test_positional_before_after_supported(self) -> None:
        doc_id = uuid4()
        target = _passage(uuid4(), doc_id, position=2, text="target")
        adapter, _s, _d, passages_repo = _build_adapter(context=([], target, []))

        await adapter.get_passage_context(target.id, 1, 1)

        passages_repo.get_context.assert_awaited_once()
        assert passages_repo.get_context.await_args.kwargs == {"before": 1, "after": 1}
