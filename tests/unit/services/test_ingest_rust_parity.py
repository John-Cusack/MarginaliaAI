"""The Rust backfill-routing seam must match the ingestion services.

`_classify` keeps its I/O order (pack check, then existence, then
dispatch) while the pure routing decision crosses; whole `Candidate`
objects compare across backends. `_recover_one` refuses empty recoveries
verbatim; `_resolve_language` prefers the supplied language like
`supplied or default`, including the empty-string fallback.

Rust-forced cases skip when the accelerator wheel is absent; the Python
path always runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from research_engine.services.ingestion import orchestrator as orchestrator_mod
from research_engine.services.ingestion import text_backfill as backfill_mod
from research_engine.services.ingestion.orchestrator import IngestionOrchestrator
from research_engine.services.ingestion.text_backfill import (
    Route,
    TextBackfillService,
)

DOC = UUID("12345678-1234-5678-1234-567812340000")


class _Dispatcher:
    """Fake module dispatcher: known suffixes resolve, the rest refuse."""

    def __init__(self, modules: dict[str, str] | None = None) -> None:
        self._modules = modules or {".txt": "plain_text", ".pdf": "docling"}

    async def dispatch(self, path: Path):
        module_id = self._modules.get(path.suffix)
        if module_id is None:
            raise ValueError(f"no parser for {path.suffix}")
        return SimpleNamespace(id=module_id)


def _row(source: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=DOC,
        title="t",
        source=source,
        document_type="letter",
        parser="plain_text",
    )


def _service(dispatcher: _Dispatcher) -> TextBackfillService:
    return TextBackfillService(None, None, dispatcher)


class TestClassifyParity:
    @pytest.mark.parametrize("backend", ["python", "rust"])
    async def test_routes(self, backend, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", backend)
        real = tmp_path / "doc.txt"
        real.write_text("hello")
        odd = tmp_path / "doc.xyz"
        odd.write_text("?")
        cases = {
            "logos:LLS:ABC:batch:b0000": (Route.UNREACHABLE, None),
            str(tmp_path / "gone.txt"): (Route.MISSING_FILE, None),
            str(tmp_path / "doc.xyz"): (Route.UNREACHABLE, None),
            str(real): (Route.FAST, "plain_text"),
        }
        service = _service(_Dispatcher())
        for source, (route, module_id) in cases.items():
            candidate = await service._classify(_row(source))
            assert candidate.route is route, source
            assert candidate.module_id == module_id, source
        slow = tmp_path / "scan.pdf"
        slow.write_text("pdf")
        candidate = await service._classify(_row(str(slow)))
        assert candidate.route is Route.SLOW
        assert candidate.module_id == "docling"

    async def test_backends_agree_whole_object(self, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        real = tmp_path / "doc.txt"
        real.write_text("hello world")
        sources = [
            "logos:LLS:ABC:batch:b0000",
            str(tmp_path / "gone.txt"),
            str(real),
        ]
        import dataclasses

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = [await _service(_Dispatcher())._classify(_row(s)) for s in sources]
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = [await _service(_Dispatcher())._classify(_row(s)) for s in sources]
        assert [dataclasses.asdict(c) for c in actual] == [dataclasses.asdict(c) for c in expected]
        assert actual[0].detail.startswith("source is a pack URI")
        assert actual[1].detail == "source file no longer exists"
        assert actual[2].size_bytes == len("hello world")

    async def test_dispatch_failure_carries_cause(self, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        odd = tmp_path / "doc.xyz"
        odd.write_text("?")
        expected = await _service(_Dispatcher())._classify(_row(str(odd)))
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await _service(_Dispatcher())._classify(_row(str(odd)))
        assert actual == expected
        assert actual.detail.startswith("no ingestion module accepts this source: ")


class _Module:
    def __init__(self, text: str | None, module_id: str = "plain_text") -> None:
        self._text = text
        self.id = module_id
        self.version = "1.0"

    async def parse(self, path: Path):
        return (self._text, "title", {})


class _Texts:
    def __init__(self) -> None:
        self.stored: list = []

    async def put(self, tx: object, *args: object) -> None:
        self.stored.append(args)


class _Tx:
    def __init__(self, conn: object) -> None:
        self.conn = conn


class _Conn:
    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Engine:
    def begin(self) -> _Conn:
        return _Conn()


class _RecoverDispatcher:
    def __init__(self, text: str | None) -> None:
        self._text = text

    async def dispatch(self, path: Path) -> _Module:
        return _Module(self._text)


def _candidate() -> SimpleNamespace:
    return SimpleNamespace(document_id=DOC, source="/doc.txt", route=Route.FAST, module_id=None)


class TestRecoverGuardParity:
    @pytest.mark.parametrize("text", ["", "   ", None])
    async def test_empty_recoveries_refused(self, text, monkeypatch):
        pytest.importorskip("marginalia_rs")
        for backend in ("python", "rust"):
            monkeypatch.setenv("RE_RUST_BACKEND", backend)
            service = TextBackfillService(_Engine(), _Texts(), _RecoverDispatcher(text))
            with pytest.raises(ValueError, match="^parser produced no text$"):
                await service._recover_one(_candidate())

    async def test_good_text_stored(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        for backend in ("python", "rust"):
            monkeypatch.setenv("RE_RUST_BACKEND", backend)
            texts = _Texts()
            service = TextBackfillService(_Engine(), texts, _RecoverDispatcher("hello"))
            await service._recover_one(_candidate())
            (stored,) = texts.stored
            assert stored[1] == "hello"


def _orchestrator(default: str | None) -> IngestionOrchestrator:
    return IngestionOrchestrator(
        docs=None,
        passages=None,
        embedding=None,
        ingestion_runs=None,
        dispatcher=None,
        engine=None,
        default_language=default,
    )


class TestResolveLanguageParity:
    @pytest.mark.parametrize(
        ("supplied", "default", "expected"),
        [
            ("he", "en", "he"),
            ("", "en", "en"),
            (None, "en", "en"),
            (None, None, None),
            ("he", None, "he"),
        ],
    )
    def test_resolution_matches(self, supplied, default, expected, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert _orchestrator(default)._resolve_language(supplied) == expected
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _orchestrator(default)._resolve_language(supplied) == expected

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert orchestrator_mod.IngestionOrchestrator is IngestionOrchestrator
        assert backfill_mod.TextBackfillService is TextBackfillService
        assert _orchestrator("en")._resolve_language("") == "en"
