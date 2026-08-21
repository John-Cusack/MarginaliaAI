"""The document structure tree, round-tripped through Postgres.

These exercise what the unit tests cannot: that `path` really is an ltree, that
the GiST-backed containment operators answer subtree and ancestor questions
correctly, and that the tree comes back in the order navigation depends on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.nodes import (
    PGDocumentNodeRepo,
)
from research_engine.domain.nodes import ROOT_PATH, build_node_tree

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

# A two-chapter book, the second with two sections and a subsection —
# enough depth that a subtree query is not the same as a child query.
SECTIONS = [
    {"char_start": 0, "char_end": 100, "heading": "Chapter One", "level": 1},
    {"char_start": 100, "char_end": 200, "heading": "Chapter Two", "level": 1},
    {"char_start": 200, "char_end": 300, "heading": "Two, First", "level": 2},
    {"char_start": 300, "char_end": 400, "heading": "Two, Second", "level": 2},
    {"char_start": 400, "char_end": 500, "heading": "Two, Second, a", "level": 3},
]
TEXT_LENGTH = 500


async def _store(engine: AsyncEngine, corpus: Corpus):
    doc_id = await corpus.add_document()
    repo = PGDocumentNodeRepo(engine)
    drafts = build_node_tree(SECTIONS, text_length=TEXT_LENGTH, title="A Book")
    async with transaction(engine) as tx:
        stored = await repo.insert_many(tx, doc_id, drafts)
    return repo, doc_id, {node.title: node for node in stored}


async def test_tree_round_trips_with_parent_links_resolved(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    repo, doc_id, by_title = await _store(engine, corpus)

    tree = await repo.get_tree(doc_id)
    assert len(tree) == len(SECTIONS) + 1  # + the synthetic root

    root = next(node for node in tree if node.path == ROOT_PATH)
    assert root.parent_id is None
    assert root.node_type == "document"
    assert (root.char_start, root.char_end) == (0, TEXT_LENGTH)

    # Every non-root node's parent_id must point at a real row, not be guessed
    # from the path string.
    ids = {node.id for node in tree}
    for node in tree:
        if node.parent_id is not None:
            assert node.parent_id in ids

    assert by_title["Two, First"].parent_id == by_title["Chapter Two"].id
    assert by_title["Two, Second, a"].parent_id == by_title["Two, Second"].id


async def test_outline_can_be_depth_limited(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """The cheap map: chapters without the sections beneath them."""
    repo, doc_id, _ = await _store(engine, corpus)

    outline = await repo.get_outline(doc_id, max_depth=1)

    assert [node.title for node in outline] == ["A Book", "Chapter One", "Chapter Two"]
    assert len(await repo.get_outline(doc_id)) == len(SECTIONS) + 1


async def test_subtree_uses_ltree_containment(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    repo, _doc_id, by_title = await _store(engine, corpus)

    subtree = await repo.get_subtree(by_title["Chapter Two"].id)

    # Chapter Two, both its sections, and the subsection under the second —
    # a child query would have returned only the first two.
    assert [node.title for node in subtree] == [
        "Chapter Two",
        "Two, First",
        "Two, Second",
        "Two, Second, a",
    ]

    assert [n.title for n in await repo.get_subtree(by_title["Chapter One"].id)] == [
        "Chapter One"
    ]


async def test_ancestors_give_a_citable_chain(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    repo, _doc_id, by_title = await _store(engine, corpus)

    chain = await repo.get_ancestors(by_title["Two, Second, a"].id)

    assert [node.title for node in chain] == [
        "A Book",
        "Chapter Two",
        "Two, Second",
        "Two, Second, a",
    ]


async def test_find_by_span_returns_the_deepest_container(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """A passage sits inside every ancestor; only the innermost is useful."""
    repo, doc_id, _ = await _store(engine, corpus)

    found = await repo.find_by_span(doc_id, 410, 420)
    assert found is not None
    assert found.title == "Two, Second, a"

    # A span straddling two chapters can only be held by something enclosing
    # both — here, the root.
    straddling = await repo.find_by_span(doc_id, 50, 250)
    assert straddling is not None
    assert straddling.node_type == "document"


async def test_a_document_with_no_structure_still_gets_a_root(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await corpus.add_document()
    repo = PGDocumentNodeRepo(engine)

    async with transaction(engine) as tx:
        await repo.insert_many(tx, doc_id, build_node_tree([], text_length=42))

    tree = await repo.get_tree(doc_id)
    assert len(tree) == 1
    assert tree[0].char_end == 42


async def test_deleting_the_document_takes_the_tree_with_it(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """The parent_id self-reference cascades, so no orphan rows survive."""
    repo, doc_id, _ = await _store(engine, corpus)
    assert await repo.get_tree(doc_id)

    async with transaction(engine) as tx:
        removed = await repo.delete_for_document(tx, doc_id)

    assert removed == len(SECTIONS) + 1
    assert await repo.get_tree(doc_id) == []


async def test_passages_are_stamped_with_their_containing_node(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """The join behind bottom-up entry: a hit, then the section it argues in."""
    from research_engine.adapters.storage.postgres.repositories.passages import (
        PGPassageRepo,
    )
    from research_engine.domain.nodes import attach_nodes
    from research_engine.domain.passages import PassageDraft

    doc_id = await corpus.add_document()
    nodes_repo = PGDocumentNodeRepo(engine)
    passages_repo = PGPassageRepo(engine)

    async with transaction(engine) as tx:
        stored = await nodes_repo.insert_many(
            tx, doc_id, build_node_tree(SECTIONS, text_length=TEXT_LENGTH)
        )

    by_title = {node.title: node for node in stored}
    drafts = [
        # Inside "Chapter One".
        PassageDraft(
            position=0, char_start=10, char_end=30, text="x" * 20,
            chunker="structural", chunker_version="3.0",
        ),
        # Inside the deepest node, "Two, Second, a".
        PassageDraft(
            position=1, char_start=410, char_end=430, text="y" * 20,
            chunker="structural", chunker_version="3.0",
        ),
    ]

    async with transaction(engine) as tx:
        saved = await passages_repo.insert_many(
            tx, doc_id, attach_nodes(drafts, stored)
        )

    assert saved[0].node_id == by_title["Chapter One"].id
    assert saved[1].node_id == by_title["Two, Second, a"].id

    # And it survives the round trip, rather than only living on the draft.
    reloaded = await passages_repo.get(saved[1].id)
    assert reloaded is not None
    assert reloaded.node_id == by_title["Two, Second, a"].id


async def test_reading_a_node_gathers_its_passages(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    from research_engine.adapters.storage.postgres.repositories.passages import (
        PGPassageRepo,
    )
    from research_engine.domain.nodes import attach_nodes
    from research_engine.domain.passages import PassageDraft

    doc_id = await corpus.add_document()
    nodes_repo = PGDocumentNodeRepo(engine)
    passages_repo = PGPassageRepo(engine)

    async with transaction(engine) as tx:
        stored = await nodes_repo.insert_many(
            tx, doc_id, build_node_tree(SECTIONS, text_length=TEXT_LENGTH)
        )
    by_title = {node.title: node for node in stored}

    # One passage in each of Chapter Two's descendants, plus its own prose.
    spans = [(110, 130), (210, 230), (310, 330), (410, 430)]
    drafts = [
        PassageDraft(
            position=index, char_start=start, char_end=end, text="z" * (end - start),
            chunker="structural", chunker_version="3.0",
        )
        for index, (start, end) in enumerate(spans)
    ]
    async with transaction(engine) as tx:
        await passages_repo.insert_many(tx, doc_id, attach_nodes(drafts, stored))

    chapter_two = by_title["Chapter Two"]

    direct = await passages_repo.get_by_node(chapter_two.id)
    assert len(direct) == 1  # only the chapter's own prose

    whole = await passages_repo.get_by_node(chapter_two.id, include_descendants=True)
    assert len(whole) == 4  # the chapter and everything beneath it
    assert [p.position for p in whole] == [0, 1, 2, 3]


async def test_rebuilding_the_tree_does_not_take_the_passages(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """SET NULL, not CASCADE — re-parsing must not destroy the passage layer."""
    from research_engine.adapters.storage.postgres.repositories.passages import (
        PGPassageRepo,
    )
    from research_engine.domain.nodes import attach_nodes
    from research_engine.domain.passages import PassageDraft

    doc_id = await corpus.add_document()
    nodes_repo = PGDocumentNodeRepo(engine)
    passages_repo = PGPassageRepo(engine)

    async with transaction(engine) as tx:
        stored = await nodes_repo.insert_many(
            tx, doc_id, build_node_tree(SECTIONS, text_length=TEXT_LENGTH)
        )
        saved = await passages_repo.insert_many(
            tx,
            doc_id,
            attach_nodes(
                [
                    PassageDraft(
                        position=0, char_start=10, char_end=30, text="x" * 20,
                        chunker="structural", chunker_version="3.0",
                    )
                ],
                stored,
            ),
        )
    assert saved[0].node_id is not None

    async with transaction(engine) as tx:
        await nodes_repo.delete_for_document(tx, doc_id)

    survivor = await passages_repo.get(saved[0].id)
    assert survivor is not None
    assert survivor.text == "x" * 20
    assert survivor.node_id is None
