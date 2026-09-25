"""Tests for IngestionOrchestrator helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from research_engine.domain.documents import Document, DocumentFilter
from research_engine.domain.errors import IngestRefused
from research_engine.services.ingestion.orchestrator import IngestionOrchestrator

if TYPE_CHECKING:
    from pathlib import Path


class _FakeDocumentRepo:
    """In-memory DocumentRepo just for find_existing tests."""

    def __init__(self, docs: list[Document]) -> None:
        self._docs = docs

    async def iter_by_filter(self, filt: DocumentFilter):
        # Mirror the postgres repo's source_pattern semantics: case-insensitive
        # substring match. No metadata filtering (postgres impl ignores it too).
        for doc in self._docs:
            if filt.source_pattern and filt.source_pattern.lower() not in doc.source.lower():
                continue
            yield doc


class _FakePassageRepo:
    def __init__(self, counts: dict[UUID, int]) -> None:
        self._counts = counts

    async def get_by_document(self, document_id: UUID):
        # Return list of length N — find_existing only uses len().
        return [object()] * self._counts.get(document_id, 0)


def _doc(source: str, *, doc_id: UUID | None = None, title: str = "Doc") -> Document:
    return Document(
        id=doc_id or uuid4(),
        title=title,
        document_type="kindle_book",
        language=None,
        source=source,
        content_hash=b"\x00" * 32,
        parser="plugin_direct",
        parser_version="1.0",
        ingested_at=datetime.now(UTC),
        metadata={},
    )


def _orchestrator(docs: list[Document], passage_counts: dict[UUID, int]) -> IngestionOrchestrator:
    return IngestionOrchestrator(
        docs=_FakeDocumentRepo(docs),
        passages=_FakePassageRepo(passage_counts),
        embedding=object(),
        ingestion_runs=object(),
        dispatcher=object(),
        engine=object(),
    )


class TestFindExisting:
    @pytest.mark.asyncio
    async def test_exact_source_match(self):
        d = _doc("/tmp/extracted/B003NX6Z3W.txt")
        orch = _orchestrator([d], {d.id: 301})
        result = await orch.find_existing(source="/tmp/extracted/B003NX6Z3W.txt")
        assert len(result) == 1
        assert result[0]["document_id"] == str(d.id)
        assert result[0]["passage_count"] == 301

    @pytest.mark.asyncio
    async def test_exact_source_rejects_substring_hit(self):
        # Two docs whose sources both contain the ASIN, but only one matches exactly.
        d1 = _doc("/tmp/extracted/B003NX6Z3W.txt")
        d2 = _doc("/tmp/extracted/B003NX6Z3W.bak.txt")
        orch = _orchestrator([d1, d2], {d1.id: 10, d2.id: 20})
        result = await orch.find_existing(source="/tmp/extracted/B003NX6Z3W.txt")
        assert len(result) == 1
        assert result[0]["document_id"] == str(d1.id)

    @pytest.mark.asyncio
    async def test_source_pattern_substring_match(self):
        d1 = _doc("/tmp/extracted/B003NX6Z3W.txt")
        d2 = _doc("/tmp/extracted/B0090NUP8K.txt")
        orch = _orchestrator([d1, d2], {d1.id: 10, d2.id: 20})
        result = await orch.find_existing(source_pattern="B003NX6Z3W")
        assert len(result) == 1
        assert result[0]["document_id"] == str(d1.id)

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self):
        d = _doc("/tmp/extracted/B003NX6Z3W.txt")
        orch = _orchestrator([d], {d.id: 301})
        result = await orch.find_existing(source_pattern="BFAKEFAKE0")
        assert result == []

    @pytest.mark.asyncio
    async def test_requires_source_or_pattern(self):
        orch = _orchestrator([], {})
        with pytest.raises(ValueError):
            await orch.find_existing()


class _FakeRuns:
    """Records whether ingest_paths ever opened a run."""

    def __init__(self) -> None:
        self.started: list[dict] = []

    async def start_run(self, params: dict) -> SimpleNamespace:
        self.started.append(params)
        return SimpleNamespace(id=uuid4())

    async def add_item(self, *args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(id=uuid4())

    async def update_item(self, *args: object, **kwargs: object) -> None:
        return None

    async def complete_run(self, *args: object, **kwargs: object) -> None:
        return None


class _FakeDocs:
    async def find_by_hash(self, *args: object) -> None:
        return None


class _FakeModule:
    id = "test_md"
    version = "1.0"

    def default_document_type(self) -> str:
        return "test_doc"

    def default_chunker(self) -> str:
        return "test_chunker"

    async def parse(self, path: Path) -> tuple[str, str, dict]:
        return (path.read_text(encoding="utf-8"), "Note", {})


class _FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    async def dispatch(self, path: Path, hint: str | None) -> _FakeModule:
        self.calls.append(path)
        return _FakeModule()


def _guard_orchestrator(
    monkeypatch: pytest.MonkeyPatch, vault: Path
) -> tuple[IngestionOrchestrator, _FakeRuns, _FakeDispatcher]:
    """An orchestrator whose pipeline is stubbed past discovery and parsing."""
    from research_engine.services.ingestion import orchestrator as module

    async def fake_chunking(*args: object, **kwargs: object) -> list:
        return []

    async def fake_store(self: object, **kwargs: object) -> tuple:
        return (SimpleNamespace(id=uuid4()), [], False)

    monkeypatch.setattr(module, "run_chunking", fake_chunking)
    monkeypatch.setattr(
        IngestionOrchestrator, "_store_file_document", fake_store
    )
    runs, dispatcher = _FakeRuns(), _FakeDispatcher()
    service = IngestionOrchestrator(
        docs=_FakeDocs(),
        passages=object(),
        embedding=object(),
        ingestion_runs=runs,
        dispatcher=dispatcher,
        engine=object(),
        forbidden_roots=[vault],
    )
    return service, runs, dispatcher


class TestForbiddenRoots:
    @pytest.mark.asyncio
    async def test_file_inside_vault_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        note = vault / "Works" / "slug.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Beat\n", encoding="utf-8")
        service, runs, _ = _guard_orchestrator(monkeypatch, vault)

        with pytest.raises(IngestRefused):
            await service.ingest_paths([note])
        assert runs.started == []

    @pytest.mark.asyncio
    async def test_vault_directory_itself_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        service, runs, _ = _guard_orchestrator(monkeypatch, vault)

        with pytest.raises(IngestRefused):
            await service.ingest_paths([vault])
        assert runs.started == []

    @pytest.mark.asyncio
    async def test_symlink_into_vault_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        secret = vault / "note.md"
        secret.write_text("# Secret\n", encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "link.md").symlink_to(secret)
        service, _, dispatcher = _guard_orchestrator(monkeypatch, vault)

        # Raising, not skipping: a vault path must never pass silently. The
        # run opened before discovery, but nothing reaches the pipeline.
        with pytest.raises(IngestRefused):
            await service.ingest_paths([outside])
        assert dispatcher.calls == []
    @pytest.mark.asyncio
    async def test_sibling_outside_vault_ingests(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        note = outside / "note.md"
        note.write_text("Outside words.\n", encoding="utf-8")
        service, runs, dispatcher = _guard_orchestrator(monkeypatch, vault)

        stats = await service.ingest_paths([outside])

        assert stats["ok"] == 1
        assert [str(call) for call in dispatcher.calls] == [str(note)]
        assert len(runs.started) == 1

    @pytest.mark.asyncio
    async def test_no_forbidden_roots_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "note.md"
        note.write_text("Inside words.\n", encoding="utf-8")
        from research_engine.services.ingestion import orchestrator as module

        async def fake_chunking(*args: object, **kwargs: object) -> list:
            return []

        async def fake_store(self: object, **kwargs: object) -> tuple:
            return (SimpleNamespace(id=uuid4()), [], False)

        monkeypatch.setattr(module, "run_chunking", fake_chunking)
        monkeypatch.setattr(
            IngestionOrchestrator, "_store_file_document", fake_store
        )
        runs, dispatcher = _FakeRuns(), _FakeDispatcher()
        service = IngestionOrchestrator(
            docs=_FakeDocs(),
            passages=object(),
            embedding=object(),
            ingestion_runs=runs,
            dispatcher=dispatcher,
            engine=object(),
        )

        stats = await service.ingest_paths([note])

        assert stats["ok"] == 1
