"""The batched span and ancestor queries, against real Postgres.

Tested as equivalence oracles against the single-item forms already in use,
rather than by asserting SQL. That catches the things unit tests with fakes
cannot: `substring` being 1-indexed, a negative start being consumed by the
length rather than rejected, and an outer join being needed so a document with
no stored text yields a hole instead of shifting every later answer onto the
wrong request.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGDocumentNodeRepo,
    PGDocumentTextRepo,
    PGPassageRepo,
)
from research_engine.domain.nodes import build_node_tree
from research_engine.domain.passages import PassageDraft
from research_engine.services.text.sections import sections_from_markdown

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

TEXT = (
    "# Justice\n\nThe prophets pair two words, and the pairing is the point.\n\n"
    "## Mishpat\n\nRendered judgement, ordinance, or custom depending on the\n"
    "translator's ear. It governs weights and measures as readily as courts.\n\n"
    "## Tsedaqah\n\nRighteousness, but relational rather than forensic.\n\n"
    "# Reception\n\nLater readers narrowed both words considerably.\n"
)


async def _ingest(engine: AsyncEngine, corpus: Corpus) -> uuid.UUID:
    doc_id = await corpus.add_document()
    sections = sections_from_markdown(TEXT)
    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(tx, doc_id, TEXT, "test", "1.0")
        await PGDocumentNodeRepo(engine).insert_many(
            tx, doc_id, build_node_tree(sections, text_length=len(TEXT), title="Justice")
        )
        await PGPassageRepo(engine).insert_many(
            tx,
            doc_id,
            [
                PassageDraft(
                    position=index,
                    char_start=section["char_start"],
                    char_end=section["char_end"],
                    text=TEXT[section["char_start"] : section["char_end"]],
                    chunker="structural",
                    chunker_version="3.0",
                )
                for index, section in enumerate(sections)
            ],
        )
    return doc_id


@pytest.mark.asyncio
async def test_get_spans_agrees_with_get_span_one_at_a_time(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)
    texts = PGDocumentTextRepo(engine)
    requests = [(doc_id, 0, 20), (doc_id, 40, 90), (doc_id, 10, 11)]

    batched = await texts.get_spans(requests)
    one_by_one = [await texts.get_span(*request) for request in requests]

    assert batched == one_by_one


@pytest.mark.asyncio
async def test_a_document_with_no_text_leaves_a_hole_rather_than_shifting(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """Why the join is an OUTER join.

    An inner join would drop the row and slide every later answer onto the wrong
    request — silently, since the results are positional.
    """
    doc_id = await _ingest(engine, corpus)
    texts = PGDocumentTextRepo(engine)

    batched = await texts.get_spans(
        [(doc_id, 0, 10), (uuid.uuid4(), 0, 10), (doc_id, 10, 20)]
    )

    assert batched[1] is None
    assert batched[0] == TEXT[0:10]
    assert batched[2] == TEXT[10:20]


@pytest.mark.asyncio
async def test_a_negative_start_is_clamped_not_consumed_by_the_length(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """`substring(t, -2, 5)` is legal SQL and returns two characters.

    So an unclamped window near offset 0 silently returns a fraction of the
    budget rather than failing.
    """
    doc_id = await _ingest(engine, corpus)

    (span,) = await PGDocumentTextRepo(engine).get_spans([(doc_id, -50, 30)])

    assert span == TEXT[0:30]


@pytest.mark.asyncio
async def test_a_span_past_the_end_returns_what_exists(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)

    (span,) = await PGDocumentTextRepo(engine).get_spans(
        [(doc_id, len(TEXT) - 10, len(TEXT) + 5_000)]
    )

    assert span == TEXT[-10:]


@pytest.mark.asyncio
async def test_get_ancestors_many_agrees_with_get_ancestors(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)
    nodes = PGDocumentNodeRepo(engine)
    tree = await nodes.get_tree(doc_id)
    deepest = sorted(tree, key=lambda n: n.depth)[-3:]
    node_ids = [n.id for n in deepest]

    batched = await nodes.get_ancestors_many(node_ids)
    one_by_one = {n: await nodes.get_ancestors(n) for n in node_ids}

    for node_id in node_ids:
        assert [a.id for a in batched[node_id]] == [a.id for a in one_by_one[node_id]]


@pytest.mark.asyncio
async def test_the_window_reads_back_exactly_what_it_claims(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """The `PassageDraft` invariant, applied on the way out.

    Catches the 1-indexing, the clamp and the trim arithmetic in one assertion.
    """
    from research_engine.services.search.windows import PassageWindowReader

    doc_id = await _ingest(engine, corpus)
    texts = PGDocumentTextRepo(engine)
    passages = await PGPassageRepo(engine).get_by_document(doc_id)
    reader = PassageWindowReader(
        PGDocumentNodeRepo(engine), texts, max_tokens=200, min_tokens=40
    )

    windows = await reader.read(passages)

    assert windows
    canonical = await texts.get_text(doc_id)
    for passage_id, window in windows.items():
        assert window.text == canonical[window.char_start : window.char_end]
        passage = next(p for p in passages if p.id == passage_id)
        assert window.char_start <= passage.char_start
        assert window.char_end >= passage.char_end
