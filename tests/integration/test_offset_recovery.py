"""Re-anchoring passages that were written without a span.

`prose_window` 1.0 recorded no offsets, and rebuilt each chunk as
`" ".join(sentences)` — so the stored text is the canonical text with its
whitespace runs flattened. That is recoverable by matching, and matching costs
no embedding, which is the whole reason this exists separately from
`reindex chunks`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.document_texts import (
    PGDocumentTextRepo,
)
from research_engine.adapters.storage.postgres.repositories.nodes import (
    PGDocumentNodeRepo,
)
from research_engine.services.ingestion.offsets import OffsetRecoveryService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = pytest.mark.asyncio

CANONICAL = (
    "## To Winfield Scott\n\n"
    "Cincinnati,   April 27 1861\n\n"
    "I assume it as the final result\nthat hostilities will break out\n"
    "on the line of the Ohio.\n\n"
    "## To Lorenzo Thomas\n\n"
    "Columbus, May 7 1861\n\n"
    "The troops are   mustering in\ngood order and spirits.\n"
)

#: What `prose_window` 1.0 stored: the same words, whitespace flattened.
FLATTENED = [
    "I assume it as the final result that hostilities will break out on the line of the Ohio.",
    "The troops are mustering in good order and spirits.",
]


async def _document_with_flattened_passages(engine: AsyncEngine, corpus: Corpus):
    doc_id = await corpus.add_document(title="Papers")
    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(tx, doc_id, CANONICAL, "test", "1.0")
    passage_ids = []
    for position, text in enumerate(FLATTENED):
        passage_ids.append(await corpus.add_passage(doc_id, text, position=position))
    # Strip the spans, as the chunker that wrote these never recorded any.
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE core.passages SET char_start = NULL, char_end = NULL "
                "WHERE document_id = :d"
            ),
            {"d": doc_id},
        )
    return doc_id, passage_ids


def a_service(engine: AsyncEngine) -> OffsetRecoveryService:
    return OffsetRecoveryService(
        engine,
        PGDocumentTextRepo(engine),
        PGDocumentNodeRepo(engine),
        lambda: transaction(engine),
    )


async def test_every_passage_gets_a_span_back(engine: AsyncEngine, corpus: Corpus):
    doc_id, _ = await _document_with_flattened_passages(engine, corpus)

    report = await a_service(engine).recover([doc_id])

    assert (report.passages_without_a_span, report.recovered) == (2, 2)
    assert report.unmatched == []


async def test_the_span_and_the_text_agree_exactly(
    engine: AsyncEngine, corpus: Corpus
):
    """The invariant every span-addressed feature rests on."""
    doc_id, _ = await _document_with_flattened_passages(engine, corpus)

    await a_service(engine).recover([doc_id])

    async with engine.connect() as conn:
        disagreements = (
            await conn.execute(
                sa.text(
                    "SELECT count(*) FROM core.passages p "
                    "JOIN core.document_texts t ON t.document_id = p.document_id "
                    "WHERE p.document_id = :d AND substring(t.text FROM p.char_start + 1 "
                    "FOR p.char_end - p.char_start) IS DISTINCT FROM p.text"
                ),
                {"d": doc_id},
            )
        ).scalar()
    assert disagreements == 0


async def test_the_source_s_own_whitespace_is_restored(
    engine: AsyncEngine, corpus: Corpus
):
    """Text only ever changes back toward what the document actually says."""
    doc_id, passage_ids = await _document_with_flattened_passages(engine, corpus)

    report = await a_service(engine).recover([doc_id])

    async with engine.connect() as conn:
        text = (
            await conn.execute(
                sa.text("SELECT text FROM core.passages WHERE id = :p"),
                {"p": passage_ids[0]},
            )
        ).scalar()
    assert report.text_restored == 2
    assert "\n" in text
    assert text.split() == FLATTENED[0].split()


async def test_a_dry_run_writes_nothing(engine: AsyncEngine, corpus: Corpus):
    doc_id, _ = await _document_with_flattened_passages(engine, corpus)

    report = await a_service(engine).recover([doc_id], dry_run=True)

    assert report.recovered == 2
    async with engine.connect() as conn:
        still_null = (
            await conn.execute(
                sa.text(
                    "SELECT count(*) FROM core.passages "
                    "WHERE document_id = :d AND char_start IS NULL"
                ),
                {"d": doc_id},
            )
        ).scalar()
    assert still_null == 2


async def test_text_that_is_not_in_the_document_is_left_alone(
    engine: AsyncEngine, corpus: Corpus
):
    """A wrong span is a citation pointing at the wrong sentence."""
    doc_id, _ = await _document_with_flattened_passages(engine, corpus)
    stray = await corpus.add_passage(doc_id, "This sentence is nowhere in it.", position=9)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE core.passages SET char_start = NULL, char_end = NULL "
                "WHERE id = :p"
            ),
            {"p": stray},
        )

    report = await a_service(engine).recover([doc_id])

    assert report.unmatched == [str(stray)]
    assert report.recovered == 2
