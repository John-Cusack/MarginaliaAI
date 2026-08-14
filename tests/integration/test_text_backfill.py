"""Recovering canonical text for documents that predate `document_texts`.

The classification matters as much as the recovery: telling a researcher that
2,704 documents are "not recoverable" and 16 are cheap is the difference between
a decision and a shrug.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.document_texts import (
    PGDocumentTextRepo,
)
from research_engine.services.ingestion.dispatch import ModuleDispatcher
from research_engine.services.ingestion.text_backfill import (
    Route,
    TextBackfillService,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

BODY = (
    "The archive holds letters from the campaign.\n\n"
    "Each letter carries a date. Some are approximate.\n"
)


def dispatcher() -> ModuleDispatcher:
    from research_engine.modules.markdown import MarkdownModule
    from research_engine.modules.plain_text import PlainTextModule

    d = ModuleDispatcher()
    d.register(PlainTextModule())
    d.register(MarkdownModule())
    return d


def service(engine: AsyncEngine) -> TextBackfillService:
    return TextBackfillService(engine, PGDocumentTextRepo(engine), dispatcher())


async def test_plain_text_source_is_classified_fast_and_recovered(
    engine: AsyncEngine, corpus: Corpus, tmp_path: Path
) -> None:
    source = tmp_path / "book.txt"
    source.write_text(BODY)
    doc = await corpus.add_document(source=str(source), document_type="ycl_book")

    plan = await service(engine).plan([doc])
    assert [c.route for c in plan] == [Route.FAST]
    assert plan[0].size_bytes == len(BODY)

    report = await service(engine).recover([doc])
    assert report.recovered == 1

    stored = await PGDocumentTextRepo(engine).get_text(doc)
    assert stored == BODY


async def test_pack_uri_is_unreachable_not_missing(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """A `logos:` batch URI is not a broken path — it is someone else's job."""
    doc = await corpus.add_document(
        source="logos:LLS:WMNMNSTRYFRVWS:batch:b0000", document_type="logos_book"
    )
    plan = await service(engine).plan([doc])
    assert plan[0].route is Route.UNREACHABLE
    assert "pack" in plan[0].detail


async def test_deleted_source_is_distinguished_from_a_pack_uri(
    engine: AsyncEngine, corpus: Corpus, tmp_path: Path
) -> None:
    doc = await corpus.add_document(source=str(tmp_path / "gone.txt"))
    plan = await service(engine).plan([doc])
    assert plan[0].route is Route.MISSING_FILE


async def test_documents_that_already_have_text_are_not_candidates(
    engine: AsyncEngine, corpus: Corpus, tmp_path: Path
) -> None:
    source = tmp_path / "book.txt"
    source.write_text(BODY)
    doc = await corpus.add_document(source=str(source))

    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(tx, doc, BODY, "test", "1.0")

    assert await service(engine).plan([doc]) == []


async def test_dry_run_writes_nothing(
    engine: AsyncEngine, corpus: Corpus, tmp_path: Path
) -> None:
    source = tmp_path / "book.txt"
    source.write_text(BODY)
    doc = await corpus.add_document(source=str(source))

    report = await service(engine).recover([doc], dry_run=True)
    assert report.recovered == 0
    assert len(report.candidates) == 1
    assert await PGDocumentTextRepo(engine).get_text(doc) is None


async def test_slow_sources_are_excluded_unless_asked_for(
    engine: AsyncEngine, corpus: Corpus, tmp_path: Path
) -> None:
    """A docling pass over a 700 MB scan should be a deliberate choice."""
    from research_engine.services.ingestion import text_backfill

    source = tmp_path / "book.txt"
    source.write_text(BODY)
    doc = await corpus.add_document(source=str(source))

    # Treat the plain-text module as slow for the duration of this test.
    original = text_backfill.SLOW_MODULES
    text_backfill.SLOW_MODULES = frozenset({"plain_text"})
    try:
        plan = await service(engine).plan([doc])
        assert plan[0].route is Route.SLOW

        default_run = await service(engine).recover([doc])
        assert default_run.recovered == 0

        opted_in = await service(engine).recover([doc], routes={Route.SLOW})
        assert opted_in.recovered == 1
    finally:
        text_backfill.SLOW_MODULES = original


async def test_one_failure_does_not_stop_the_run(
    engine: AsyncEngine, corpus: Corpus, tmp_path: Path
) -> None:
    good_source = tmp_path / "good.txt"
    good_source.write_text(BODY)
    empty_source = tmp_path / "empty.txt"
    empty_source.write_text("   \n  ")

    good = await corpus.add_document(source=str(good_source))
    empty = await corpus.add_document(source=str(empty_source))

    report = await service(engine).recover([good, empty])
    assert report.recovered == 1
    assert str(empty) in str(report.failed) or len(report.failed) == 1
    assert await PGDocumentTextRepo(engine).get_text(good) == BODY
