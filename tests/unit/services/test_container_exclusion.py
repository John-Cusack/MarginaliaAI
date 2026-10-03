"""A container whose units are documents of their own stays out of default search.

Once a collected volume's letters are split out as `letter` documents, the
volume's text holds every letter a second time. Unless something keeps it out,
every query about a letter returns it twice — once as the letter, once inside
the volume — and the volume's copy, cut by chunk length rather than at the
letter's edges, mixes the letter with its neighbours. Convention does not hold:
hybrid search narrows only when a caller passes filters, so exclusion has to be
the default.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from research_engine.adapters.storage.postgres.repositories.passages import (
    build_candidate_stmt,
)
from research_engine.domain.passages import SearchFilters, SearchQuery
from research_engine.plugins.registry import PluginRegistry
from research_engine.services.search.hybrid import HybridSearchService
from research_engine_sdk.manifest import parse_manifest_bytes

IDS = [uuid.uuid4() for _ in range(3)]


class RecordingPassages:
    def __init__(self) -> None:
        self.vector_kwargs: dict = {}
        self.keyword_kwargs: dict = {}
        self.candidate_filters: dict | None = None

    async def vector_search(self, vec, model, version, candidates, k, **kwargs):
        self.vector_kwargs = kwargs
        return [(pid, 1.0) for pid in IDS[:k]]

    async def keyword_search(self, text, lang, candidates, k, **kwargs):
        self.keyword_kwargs = kwargs
        return [(pid, 0.5) for pid in IDS[:k]]

    async def filter_candidate_ids(self, filters, filter_extensions=None):
        self.candidate_filters = filters
        return IDS

    async def get_many(self, passage_ids):
        return [
            SimpleNamespace(
                id=pid, document_id=uuid.uuid4(), text="t", metadata={}, locator={},
                char_start=0, char_end=1, node_id=None,
            )
            for pid in passage_ids
        ]


class FakeEmbedding:
    model_name, model_version, dim = "BAAI/bge-m3", "1.0", 1024

    async def embed(self, text):
        return [0.1] * self.dim


def a_search(passages: RecordingPassages) -> HybridSearchService:
    return HybridSearchService(
        passages=passages,
        embedding=FakeEmbedding(),
        reranker=None,
        get_unsearchable_types=lambda: ["letter_collection"],
    )


async def test_an_unfiltered_search_leaves_containers_out():
    passages = RecordingPassages()
    result = await a_search(passages).find_passages(
        SearchQuery(text="Geneva", k=3, rerank=False)
    )

    assert passages.vector_kwargs == {"exclude_document_types": ["letter_collection"]}
    assert passages.keyword_kwargs == {"exclude_document_types": ["letter_collection"]}
    assert passages.candidate_filters is None, "no full-corpus candidate scan"
    assert result.applied_filters == {"exclude_document_types": ["letter_collection"]}


async def test_naming_a_container_type_searches_it():
    passages = RecordingPassages()
    await a_search(passages).find_passages(
        SearchQuery(
            text="Geneva",
            k=3,
            rerank=False,
            filters=SearchFilters(document_types=["letter_collection"]),
        )
    )

    assert "exclude_document_types" not in passages.candidate_filters
    assert passages.vector_kwargs == {}


async def test_another_filter_carries_the_exclusion_into_the_candidates():
    passages = RecordingPassages()
    await a_search(passages).find_passages(
        SearchQuery(
            text="Geneva", k=3, rerank=False, filters=SearchFilters(language="en")
        )
    )

    assert passages.candidate_filters["exclude_document_types"] == ["letter_collection"]
    assert passages.vector_kwargs == {}, "the candidate list already applied it"


async def test_with_no_containers_registered_nothing_changes():
    passages = RecordingPassages()
    service = HybridSearchService(
        passages=passages, embedding=FakeEmbedding(), reranker=None
    )
    result = await service.find_passages(SearchQuery(text="Geneva", k=3, rerank=False))

    assert passages.vector_kwargs == {}
    assert result.applied_filters == {}


def test_the_candidate_statement_excludes_by_type():
    stmt = build_candidate_stmt({"exclude_document_types": ["letter_collection"]})
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "NOT IN" in sql
    assert "document_type" in sql
    assert isinstance(stmt, sa.Select)


def test_a_pack_declares_a_type_unsearchable():
    registry = PluginRegistry()
    registry.register_document_type("letter", {"searchable": True}, "history")
    registry.register_document_type(
        "letter_collection", {"searchable": False}, "history"
    )
    registry.register_document_type("generic_thing", {}, "other")

    assert registry.unsearchable_document_types() == ["letter_collection"]


def test_the_manifest_carries_the_flag():
    manifest = parse_manifest_bytes(
        b"""
schema_version: 2
plugin_id: probe
requires:
  core_api: ">=0.6,<0.7"
provides:
  document_types:
    - id: letter_collection
      default_chunker: structural
      searchable: false
    - id: letter
"""
    )
    types = {t.id: t for t in manifest.provides.document_types}
    assert types["letter_collection"].searchable is False
    assert types["letter"].searchable is True
