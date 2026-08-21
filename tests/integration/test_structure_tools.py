"""The structure MCP tools, against a real corpus.

One tool per way in: `get_document_outline` for "where is this discussed",
`read_node` for "what does this part argue", `locate_passage` for "where did
this hit come from". They are exercised through their handlers rather than
their repositories, because the handler is what an agent actually meets — its
error shapes and its refusals are the contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.document_texts import (
    PGDocumentTextRepo,
)
from research_engine.adapters.storage.postgres.repositories.nodes import (
    PGDocumentNodeRepo,
)
from research_engine.adapters.storage.postgres.repositories.passages import PGPassageRepo
from research_engine.domain.nodes import attach_nodes, build_node_tree
from research_engine.domain.passages import PassageDraft
from research_engine.mcp.tools import get_document_outline, locate_passage, read_node
from research_engine.services.text.sections import sections_from_markdown

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

TEXT = """# Chapter One

The archive holds letters from the period.

## Provenance

Ownership passed through three families before the bequest.

# Chapter Two

The second chapter concerns the binding and its repair.
"""


def _container(engine: AsyncEngine) -> SimpleNamespace:
    return SimpleNamespace(
        document_nodes=PGDocumentNodeRepo(engine),
        document_texts=PGDocumentTextRepo(engine),
        passage_repo=PGPassageRepo(engine),
    )


async def _ingest(engine: AsyncEngine, corpus: Corpus):
    """A document with canonical text, a tree, and node-stamped passages."""
    doc_id = await corpus.add_document()
    sections = sections_from_markdown(TEXT)
    nodes_repo = PGDocumentNodeRepo(engine)

    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(tx, doc_id, TEXT, "test", "1.0")
        nodes = await nodes_repo.insert_many(
            tx, doc_id, build_node_tree(sections, text_length=len(TEXT), title="A Book")
        )
        drafts = [
            PassageDraft(
                position=index,
                char_start=section["char_start"],
                char_end=section["char_end"],
                text=TEXT[section["char_start"] : section["char_end"]],
                chunker="structural",
                chunker_version="3.0",
            )
            for index, section in enumerate(sections)
        ]
        passages = await PGPassageRepo(engine).insert_many(
            tx, doc_id, attach_nodes(drafts, nodes)
        )
    return doc_id, {n.title: n for n in nodes}, passages


async def test_outline_is_depth_limited_and_reports_size(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id, _nodes, _passages = await _ingest(engine, corpus)

    result = await get_document_outline.handler(
        _container(engine), document_id=str(doc_id), max_depth=1
    )

    assert [n["title"] for n in result["nodes"]] == [
        "A Book",
        "Chapter One",
        "Chapter Two",
    ]
    # char_length is what tells a caller whether read_node will be cheap.
    assert all(n["char_length"] > 0 for n in result["nodes"])

    deeper = await get_document_outline.handler(
        _container(engine), document_id=str(doc_id), max_depth=2
    )
    assert "Provenance" in [n["title"] for n in deeper["nodes"]]


async def test_outline_of_an_unstructured_document_says_so(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """Absence must not read as 'this document is empty'."""
    doc_id = await corpus.add_document()

    result = await get_document_outline.handler(
        _container(engine), document_id=str(doc_id)
    )

    assert result["nodes"] == []
    assert "find_passages" in result["note"]


async def test_read_node_returns_prose_and_a_breadcrumb(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    _doc_id, nodes, _passages = await _ingest(engine, corpus)

    result = await read_node.handler(
        _container(engine), node_id=str(nodes["Provenance"].id)
    )

    assert result["breadcrumb"] == ["A Book", "Chapter One", "Provenance"]
    assert "three families" in result["text"]
    assert result["text"] == TEXT[result["char_start"] : result["char_end"]]


async def test_read_node_can_exclude_descendants(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """Chapter One's own prose stops where Provenance begins."""
    _doc_id, nodes, _passages = await _ingest(engine, corpus)
    container = _container(engine)

    whole = await read_node.handler(container, node_id=str(nodes["Chapter One"].id))
    own = await read_node.handler(
        container, node_id=str(nodes["Chapter One"].id), include_descendants=False
    )

    assert "three families" in whole["text"]
    assert "three families" not in own["text"]
    assert "archive holds letters" in own["text"]


async def test_read_node_refuses_rather_than_truncates(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """Silent truncation would have the caller reason about a partial argument."""
    _doc_id, nodes, _passages = await _ingest(engine, corpus)

    result = await read_node.handler(
        _container(engine), node_id=str(nodes["A Book"].id), max_chars=10
    )

    assert result["error"]["code"] == "node_too_large"
    assert result["error"]["details"]["limit"] == 10


async def test_read_node_reports_a_missing_node(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    result = await read_node.handler(
        _container(engine), node_id="00000000-0000-0000-0000-000000000000"
    )

    assert result["error"]["code"] == "node_not_found"


async def test_locate_passage_groups_hits_by_place(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """Repeated terminology means fewer discussions than hits."""
    _doc_id, nodes, passages = await _ingest(engine, corpus)

    result = await locate_passage.handler(
        _container(engine), passage_ids=[str(p.id) for p in passages]
    )

    assert len(result["located"]) == len(passages)
    assert result["distinct_nodes"] == len(passages)

    provenance = next(
        entry
        for entry in result["located"]
        if entry["node_id"] == str(nodes["Provenance"].id)
    )
    assert provenance["citation"] == "A Book > Chapter One > Provenance"


async def test_locate_passage_separates_the_structureless(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """A passage with no node is reported, not silently dropped."""
    _doc_id, _nodes, passages = await _ingest(engine, corpus)
    bare_doc = await corpus.add_document()
    bare = await corpus.add_passage(bare_doc, "Prose with no structure above it.")

    result = await locate_passage.handler(
        _container(engine), passage_ids=[str(passages[0].id), str(bare)]
    )

    assert [e["passage_id"] for e in result["located"]] == [str(passages[0].id)]
    assert result["unlocated"] == [str(bare)]
    assert "ingested before structure" in result["note"]
