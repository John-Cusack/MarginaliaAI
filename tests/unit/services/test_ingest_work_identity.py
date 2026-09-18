"""File identity deduplicates works without collapsing plugin components."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from research_engine.domain.documents import DocumentDraft
from research_engine.domain.passages import PassageDraft
from research_engine.services.ingestion.orchestrator import IngestionOrchestrator

pytestmark = pytest.mark.unit


class FakeDocRepo:
    def __init__(self) -> None:
        self.rows: list[tuple[Any, Any]] = []

    async def find_by_hash(self, content_hash: bytes, source: str) -> Any | None:
        return next(
            (
                document
                for document, draft in self.rows
                if draft.content_hash == content_hash and draft.source == source
            ),
            None,
        )

    async def find_by_edition_id(self, _tx: Any, edition_id: UUID) -> Any | None:
        return next(
            (
                document
                for document, draft in self.rows
                if draft.edition_id == edition_id
            ),
            None,
        )

    async def insert(self, _tx: Any, draft: DocumentDraft) -> Any:
        document = SimpleNamespace(id=uuid4())
        self.rows.append((document, draft))
        return document


class FakeEditionRepo:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.locked: list[str] = []

    async def upsert_key(
        self, _tx: Any, edition_key: str, *, lock: bool = False
    ) -> Any:
        edition = self.rows.setdefault(
            edition_key,
            SimpleNamespace(id=uuid4(), edition_key=edition_key),
        )
        if lock:
            self.locked.append(edition_key)
        return edition


class FakePassageRepo:
    def __init__(self) -> None:
        self.by_document: dict[UUID, list[Any]] = {}

    async def insert_many(
        self, _tx: Any, document_id: UUID, drafts: list[PassageDraft]
    ) -> list[Any]:
        rows = [SimpleNamespace(id=uuid4(), text=draft.text) for draft in drafts]
        self.by_document[document_id] = rows
        return rows

    async def get_by_document(self, document_id: UUID) -> list[Any]:
        return self.by_document.get(document_id, [])

    async def store_embeddings(self, *_args: Any, **_kwargs: Any) -> None: ...

    async def index_fts(self, *_args: Any, **_kwargs: Any) -> None: ...


class FakeEmbedding:
    model_name = "fake"
    model_version = "1"
    dim = 2

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0] for _text in texts]


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> tuple[IngestionOrchestrator, FakeDocRepo]:
    from research_engine.services.ingestion import orchestrator as module

    @asynccontextmanager
    async def fake_transaction(_engine: object):
        yield SimpleNamespace(conn=object())

    monkeypatch.setattr(module, "transaction", fake_transaction)
    documents = FakeDocRepo()
    orchestrator = IngestionOrchestrator(
        docs=documents,
        passages=FakePassageRepo(),
        embedding=FakeEmbedding(),
        ingestion_runs=object(),
        dispatcher=object(),
        engine=object(),
        editions=FakeEditionRepo(),
    )
    return orchestrator, documents


def passage(text: str) -> PassageDraft:
    return PassageDraft(
        position=0,
        char_start=0,
        char_end=len(text),
        text=text,
        chunker="test",
        chunker_version="1",
    )


def document(source: str, content_hash: bytes) -> DocumentDraft:
    return DocumentDraft(
        title="Article",
        source=source,
        content_hash=content_hash,
        parser="test",
        parser_version="1",
    )


async def store(
    service: IngestionOrchestrator,
    draft: DocumentDraft,
    metadata: dict[str, Any],
    text: str,
) -> tuple[Any, list[Any], bool]:
    return await service._store_file_document(  # noqa: SLF001 - transactional contract
        draft=draft,
        metadata=metadata,
        full_text=text,
        module=SimpleNamespace(id="test", version="1"),
        passage_drafts=[passage(text)],
        title="Article",
        language="en",
    )


async def test_different_artifacts_with_one_edition_become_one_document(service) -> None:
    orchestrator, documents = service
    metadata = {"edition_key": "doi:10.1000/article"}

    first, _, first_duplicate = await store(
        orchestrator, document("/downloads/a.pdf", b"first"), metadata, "first text"
    )
    second, _, second_duplicate = await store(
        orchestrator, document("/library/a.pdf", b"second"), metadata, "second text"
    )

    assert not first_duplicate
    assert second_duplicate
    assert second.id == first.id
    assert len(documents.rows) == 1


async def test_identifierless_documents_keep_artifact_identity(service) -> None:
    orchestrator, documents = service

    await store(orchestrator, document("/a.pdf", b"first"), {}, "first text")
    await store(orchestrator, document("/b.pdf", b"second"), {}, "second text")

    assert len(documents.rows) == 2


async def test_plugin_components_link_without_collapsing_the_edition(service) -> None:
    orchestrator, documents = service
    metadata = {"edition_key": "ESV"}

    await orchestrator.ingest_drafts(
        "Genesis",
        "logos_book",
        [passage("Genesis text")],
        source="logos:ESV:Genesis",
        metadata=metadata,
        full_text="Genesis text",
    )
    await orchestrator.ingest_drafts(
        "Exodus",
        "logos_book",
        [passage("Exodus text")],
        source="logos:ESV:Exodus",
        metadata=metadata,
        full_text="Exodus text",
    )

    assert len(documents.rows) == 2
    assert len({draft.edition_id for _, draft in documents.rows}) == 1
