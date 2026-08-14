"""Canonical text and passage offsets, round-tripped through Postgres."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.document_texts import (
    PGDocumentTextRepo,
)
from research_engine.adapters.storage.postgres.repositories.passages import PGPassageRepo
from research_engine.services.ingestion.chunking.prose_window import ProseWindowChunker
from research_engine.services.text.normalize import NORMALIZATION_VERSION

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

DOC_TEXT = (
    "The archive holds letters.\n\n"
    "Each letter carries a date. Some dates are approximate. "
    "Others are exact, recorded by the clerk who filed them.\n\n"
    "A final paragraph closes the folder."
)


async def test_canonical_text_round_trips(engine: AsyncEngine, corpus: Corpus) -> None:
    repo = PGDocumentTextRepo(engine)
    doc_id = await corpus.add_document()

    async with transaction(engine) as tx:
        await repo.put(tx, doc_id, DOC_TEXT, "docling", "2.1.0")

    stored = await repo.get(doc_id)
    assert stored is not None
    assert stored.text == DOC_TEXT
    assert stored.parser == "docling"
    assert stored.parser_version == "2.1.0"
    assert stored.normalization_version == NORMALIZATION_VERSION
    # Normalization is lossy on purpose; raw is what offsets address.
    assert "\n\n" not in stored.normalized_text
    assert "\n\n" in stored.text


async def test_reparsing_replaces_rather_than_conflicts(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    repo = PGDocumentTextRepo(engine)
    doc_id = await corpus.add_document()

    async with transaction(engine) as tx:
        await repo.put(tx, doc_id, DOC_TEXT, "docling", "2.1.0")
    async with transaction(engine) as tx:
        await repo.put(tx, doc_id, "Re-parsed, differently.", "docling", "2.2.0")

    stored = await repo.get(doc_id)
    assert stored.text == "Re-parsed, differently."
    assert stored.parser_version == "2.2.0"


async def test_passage_offsets_survive_the_round_trip(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """The whole point: what comes back out still addresses the stored text."""
    text_repo = PGDocumentTextRepo(engine)
    passage_repo = PGPassageRepo(engine)
    doc_id = await corpus.add_document()

    drafts = await ProseWindowChunker(max_tokens=15, overlap_tokens=3).chunk(DOC_TEXT)
    assert len(drafts) > 1, "need multiple chunks for this to mean anything"

    async with transaction(engine) as tx:
        await text_repo.put(tx, doc_id, DOC_TEXT, "test", "1.0")
        await passage_repo.insert_many(tx, doc_id, drafts)

    canonical = await text_repo.get_text(doc_id)
    for passage in await passage_repo.get_by_document(doc_id):
        assert passage.char_start is not None
        assert canonical[passage.char_start : passage.char_end] == passage.text


async def test_documents_without_canonical_text_are_reported(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    repo = PGDocumentTextRepo(engine)
    with_text = await corpus.add_document()
    without_text = await corpus.add_document()

    async with transaction(engine) as tx:
        await repo.put(tx, with_text, DOC_TEXT, "test", "1.0")

    missing = await repo.missing_document_ids()
    assert without_text in missing
    assert with_text not in missing


async def test_canonical_text_is_deleted_with_its_document(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    repo = PGDocumentTextRepo(engine)
    doc_id = await corpus.add_document()
    async with transaction(engine) as tx:
        await repo.put(tx, doc_id, DOC_TEXT, "test", "1.0")

    await corpus.cleanup()
    assert await repo.get(doc_id) is None
