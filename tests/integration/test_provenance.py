"""Provenance and marks round-trip through the database.

What a loader needs from core before it can build a corpus deliberately: an
edition registered with its terms, a blocked source that says why, raw bytes
recorded by a digest core computes itself, coverage judged against the
source's known shape, and ranges (pages, supplied words) that the node tree
cannot hold — all through the same SDK adapter a pack receives.
"""

from __future__ import annotations

import hashlib
import uuid
from functools import partial
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from research_engine.adapters.bibliography_client import BibliographyServiceAdapter
from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGAcquisitionRepo,
    PGDocumentMarkRepo,
    PGDocumentRepo,
    PGDocumentTextRepo,
    PGEditionRepo,
    PGPassageRepo,
)
from research_engine.adapters.storage.postgres.schema import (
    acquisitions,
    artifacts,
    document_texts,
    documents,
)
from research_engine.domain.acquisitions import DocumentMarkDraft
from research_engine.domain.passages import PassageDraft
from research_engine.services.ingestion.orchestrator import IngestionOrchestrator

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

# Two pages of an 1835 printing, the second break falling mid-lecture.
TEXT = (
    "LECTURE FIFTH. Of Faith. Section V. We shall in this lecture speak of the "
    "Godhead; we mean the Father, Son and Holy Spirit. "
    "There are two personages who constitute the great, matchless, governing "
    "and supreme power over all things."
)
PAGE_BREAK = TEXT.index("There are two")


def key() -> str:
    return f"TEST-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def client(engine: AsyncEngine, tmp_path: Path) -> BibliographyServiceAdapter:
    return BibliographyServiceAdapter(
        editions=PGEditionRepo(engine),
        acquisitions=PGAcquisitionRepo(engine),
        transaction_factory=partial(transaction, engine),
        data_dir=tmp_path,
    )


async def adopt(corpus: Corpus, engine: AsyncEngine, edition_key: str) -> None:
    edition = await PGEditionRepo(engine).get_by_key(edition_key)
    assert edition is not None
    corpus.adopt_edition(edition.id)


class _Embedding:
    model_name = "test"
    model_version = "1"
    dim = 1024

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 1024 for _ in texts]


def orchestrator(engine: AsyncEngine) -> IngestionOrchestrator:
    return IngestionOrchestrator(
        docs=PGDocumentRepo(engine),
        passages=PGPassageRepo(engine),
        embedding=_Embedding(),  # type: ignore[arg-type]
        ingestion_runs=None,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        engine=engine,
        document_texts=PGDocumentTextRepo(engine),
        editions=PGEditionRepo(engine),
        document_marks=PGDocumentMarkRepo(engine),
    )


async def ingest(engine: AsyncEngine, corpus: Corpus, edition_key: str, **kwargs):
    result = await orchestrator(engine).ingest_drafts(
        "Lectures on Faith, 1835",
        "test_doc",
        [
            PassageDraft(
                position=0, char_start=0, char_end=len(TEXT), text=TEXT,
                chunker="test", chunker_version="1.0",
            )
        ],
        source=f"test://{edition_key}",
        metadata={"edition_key": edition_key},
        language="en",
        full_text=TEXT,
        **kwargs,
    )
    doc_id = uuid.UUID(result["document_id"])
    corpus.adopt(doc_id)
    return doc_id, result


async def test_reregistering_keeps_progress(
    client: BibliographyServiceAdapter, engine: AsyncEngine, corpus: Corpus
) -> None:
    """Re-reading a manifest must not send a loaded source back to `planned`."""
    edition_key = key()
    first = await client.register_source(
        edition_key, method="bulk_download", url="https://example.org/a",
        license="unverified", manifest_ref="pack:test#a", work_key="test-work",
    )
    await adopt(corpus, engine, edition_key)
    assert first["status"] == "planned"

    await client.set_status(edition_key, "loaded", retrieved_at="2026-10-13T12:00:00+00:00")
    again = await client.register_source(
        edition_key, method="bulk_download", url="https://example.org/a",
        license="public domain", manifest_ref="pack:test#a",
    )

    assert again["status"] == "loaded"
    assert again["license"] == "public domain"
    assert again["retrieved_at"] is not None
    assert again["id"] == first["id"]
    # Saying nothing about the work does not forget it.
    assert (await client.get_source(edition_key))["work_key"] == "test-work"


async def test_a_blocked_source_says_why_and_leaves_the_queue_when_got(
    client: BibliographyServiceAdapter, engine: AsyncEngine, corpus: Corpus
) -> None:
    edition_key = key()
    with pytest.raises(ValueError, match="blocked_reason"):
        await client.register_source(edition_key, status="blocked")

    blocked = await client.mark_blocked(
        edition_key,
        reason="in copyright; print only",
        suggested_acquisition="scan",
        attributes={"title": "The Earliest Text"},
    )
    await adopt(corpus, engine, edition_key)
    assert blocked["status"] == "blocked"
    queue = {s["edition_key"] for s in await client.list_sources(status="blocked")}
    assert edition_key in queue

    # The database refuses a reasonless block even past the domain check.
    async with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            await conn.execute(
                acquisitions.update()
                .where(acquisitions.c.id == uuid.UUID(blocked["id"]))
                .values(blocked_reason=None)
            )

    got = await client.set_status(edition_key, "acquired")
    assert got["blocked_reason"] is None
    queue = {s["edition_key"] for s in await client.list_sources(status="blocked")}
    assert edition_key not in queue


async def test_artifacts_are_digested_here_and_stored_once(
    client: BibliographyServiceAdapter,
    engine: AsyncEngine,
    corpus: Corpus,
    tmp_path: Path,
) -> None:
    edition_key = key()
    await client.register_source(edition_key, method="scrape")
    await adopt(corpus, engine, edition_key)
    raw = tmp_path / "raw" / "page.html"
    raw.parent.mkdir()
    raw.write_bytes(b"<html>Section V</html>")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="sha256"):
        await client.attach_artifact(edition_key, raw, sha256="0" * 64)

    one = await client.attach_artifact(edition_key, raw, sha256=digest, url="https://x")
    two = await client.attach_artifact(edition_key, raw)

    assert one["sha256"] == digest
    assert one["path"] == "raw/page.html", "paths under data_dir are stored relative"
    assert one["id"] == two["id"]
    async with engine.connect() as conn:
        count = (
            await conn.execute(
                sa.select(sa.func.count()).where(
                    artifacts.c.acquisition_id == uuid.UUID(one["acquisition_id"])
                )
            )
        ).scalar_one()
    assert count == 1


async def test_coverage_is_judged_not_asserted(
    client: BibliographyServiceAdapter, engine: AsyncEngine, corpus: Corpus
) -> None:
    edition_key = key()
    await client.register_source(edition_key)
    await adopt(corpus, engine, edition_key)

    full = await client.report_coverage(
        edition_key, unit="surah", loaded_units=114, expected_units=114,
        checker="test", checker_version="1",
    )
    short = await client.report_coverage(
        edition_key, unit="ayah", loaded_units=6235, expected_units=6236,
        checker="test", checker_version="1",
    )
    odd = await client.report_coverage(
        edition_key, unit="page", loaded_units=10, anomalies=["page 7 blank"],
        checker="test", checker_version="1",
    )

    assert [full["ok"], short["ok"], odd["ok"]] == [True, False, False]
    source = await client.get_source(edition_key)
    assert [c["unit"] for c in source["coverage"]] == ["surah", "ayah", "page"]


async def test_ingest_records_parser_identity_and_marks(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    edition_key = key()
    doc_id, result = await ingest(
        engine,
        corpus,
        edition_key,
        parser="test.lectures",
        parser_version="2.1",
        mark_drafts=[
            DocumentMarkDraft(kind="page", char_start=0, char_end=0, attrs={"page": 52}),
            DocumentMarkDraft(
                kind="page", char_start=PAGE_BREAK, char_end=PAGE_BREAK,
                attrs={"page": 53},
            ),
            DocumentMarkDraft(kind="supplied", char_start=4, char_end=9),
        ],
    )
    edition = await PGEditionRepo(engine).get_by_key(edition_key)
    corpus.adopt_edition(edition.id)

    assert result["mark_count"] == 3
    async with engine.connect() as conn:
        doc = (await conn.execute(documents.select().where(documents.c.id == doc_id))).one()
        text_row = (
            await conn.execute(
                document_texts.select().where(document_texts.c.document_id == doc_id)
            )
        ).one()
    assert (doc.parser, doc.parser_version) == ("test.lectures", "2.1")
    assert (text_row.parser, text_row.parser_version) == ("test.lectures", "2.1")

    marks = PGDocumentMarkRepo(engine)
    second_page = TEXT.index("supreme power")
    on = await marks.overlapping(doc_id, second_page, second_page + 5, kind="page")
    assert [m.attrs["page"] for m in on] == [], "a page break is a point, not the page"
    spanning = await marks.overlapping(doc_id, PAGE_BREAK - 10, PAGE_BREAK + 10, kind="page")
    assert [m.attrs["page"] for m in spanning] == [53]
    assert await marks.count(doc_id, kind="supplied") == 1


async def test_a_mark_past_the_text_is_refused_before_anything_is_written(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    edition_key = key()
    with pytest.raises(ValueError, match="runs past the end"):
        await ingest(
            engine,
            corpus,
            edition_key,
            mark_drafts=[
                DocumentMarkDraft(kind="page", char_start=0, char_end=len(TEXT) + 1)
            ],
        )
    assert await PGEditionRepo(engine).get_by_key(edition_key) is None
