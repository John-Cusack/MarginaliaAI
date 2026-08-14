"""`ingest_drafts` must identify a document by its content.

It hashed `source:title`, so the same file ingested under a differently-cased
title produced a second document — the `(content_hash, source)` unique
constraint saw two different hashes and let both through. One library book is in
the corpus twice because of it.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid4

import pytest

from research_engine.domain.passages import PassageDraft
from research_engine.services.ingestion.orchestrator import IngestionOrchestrator

pytestmark = pytest.mark.unit

TEXT = "The archive holds letters. Each letter carries a date."


def draft(position: int = 0, text: str = TEXT) -> PassageDraft:
    return PassageDraft(
        position=position,
        char_start=0,
        char_end=len(text),
        text=text,
        chunker="test",
        chunker_version="2.0",
        token_count=8,
    )


class FakeDoc:
    def __init__(self, doc_id: UUID) -> None:
        self.id = doc_id


class FakeDocRepo:
    """Enforces the real (content_hash, source) uniqueness."""

    def __init__(self) -> None:
        self.rows: dict[tuple[bytes, str], FakeDoc] = {}
        self.inserts = 0

    async def find_by_hash(self, content_hash: bytes, source: str) -> FakeDoc | None:
        return self.rows.get((content_hash, source))

    async def insert(self, tx: Any, draft_doc: Any) -> FakeDoc:
        key = (draft_doc.content_hash, draft_doc.source)
        if key in self.rows:
            raise AssertionError("unique constraint violated: duplicate document")
        doc = FakeDoc(uuid4())
        self.rows[key] = doc
        self.inserts += 1
        return doc


class FakePassageRepo:
    def __init__(self) -> None:
        self.by_document: dict[UUID, list[Any]] = {}

    async def insert_many(self, tx: Any, document_id: UUID, drafts: list[Any]) -> list[Any]:
        saved = [
            type("P", (), {"id": uuid4(), "text": d.text})() for d in drafts
        ]
        self.by_document[document_id] = saved
        return saved

    async def get_by_document(self, document_id: UUID) -> list[Any]:
        return self.by_document.get(document_id, [])

    async def store_embeddings(self, *args: Any, **kwargs: Any) -> None: ...
    async def index_fts(self, *args: Any, **kwargs: Any) -> None: ...


class FakeEmbedding:
    model_name = "fake"
    model_version = "1.0"
    dim = 4

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


class FakeTx:
    def __init__(self) -> None:
        self.conn = None


class FakeEngine:
    """Stands in for AsyncEngine so `transaction()` yields something usable."""


@pytest.fixture
def orchestrator(monkeypatch: pytest.MonkeyPatch):
    from contextlib import asynccontextmanager

    from research_engine.services.ingestion import orchestrator as module

    @asynccontextmanager
    async def fake_transaction(_engine):
        yield FakeTx()

    monkeypatch.setattr(module, "transaction", fake_transaction)

    docs, passages = FakeDocRepo(), FakePassageRepo()
    service = IngestionOrchestrator(
        docs=docs,
        passages=passages,
        embedding=FakeEmbedding(),
        ingestion_runs=object(),
        dispatcher=object(),
        engine=FakeEngine(),
    )
    return service, docs, passages


async def test_same_content_and_source_is_not_ingested_twice(orchestrator) -> None:
    service, docs, _ = orchestrator

    first = await service.ingest_drafts(
        "The Shortest History of Scandinavia", "ycl_book", [draft()],
        source="/extracted/a1mynhz9.txt", full_text=TEXT,
    )
    second = await service.ingest_drafts(
        # Same file, same content, cosmetically different title — the exact
        # shape of the duplicate found in the corpus.
        "THE SHORTEST HISTORY OF SCANDINAVIA", "ycl_book", [draft()],
        source="/extracted/a1mynhz9.txt", full_text=TEXT,
    )

    assert docs.inserts == 1
    assert second["document_id"] == first["document_id"]
    assert second["skipped"] == "duplicate"


async def test_different_content_at_the_same_source_still_ingests(orchestrator) -> None:
    """A re-extraction that genuinely changed is a new document, not a dupe."""
    service, docs, _ = orchestrator

    await service.ingest_drafts(
        "Book", "ycl_book", [draft()], source="/extracted/x.txt", full_text=TEXT
    )
    await service.ingest_drafts(
        "Book", "ycl_book", [draft(text="Different text entirely.")],
        source="/extracted/x.txt", full_text="Different text entirely.",
    )
    assert docs.inserts == 2


async def test_same_content_at_different_sources_is_not_deduped(orchestrator) -> None:
    """Two batches of a book share a source prefix but are distinct documents."""
    service, docs, _ = orchestrator

    await service.ingest_drafts(
        "Vol (batch b0000)", "logos_book", [draft()],
        source="logos:LLS:X:batch:b0000", full_text=TEXT,
    )
    await service.ingest_drafts(
        "Vol (batch b0001)", "logos_book", [draft()],
        source="logos:LLS:X:batch:b0001", full_text=TEXT,
    )
    assert docs.inserts == 2


async def test_hash_is_over_content_not_metadata(orchestrator) -> None:
    service, docs, _ = orchestrator

    await service.ingest_drafts(
        "Any Title", "ycl_book", [draft()], source="/extracted/y.txt", full_text=TEXT
    )
    stored_hash = next(iter(docs.rows))[0]
    assert stored_hash == hashlib.sha256(TEXT.encode()).digest()


async def test_falls_back_to_draft_text_without_full_text(orchestrator) -> None:
    """Packs that have not adopted full_text still get content-based identity."""
    service, docs, _ = orchestrator

    await service.ingest_drafts(
        "Any Title", "ycl_book", [draft(0, "one"), draft(1, "two")],
        source="/extracted/z.txt",
    )
    stored_hash = next(iter(docs.rows))[0]
    assert stored_hash == hashlib.sha256(b"one\ntwo").digest()
