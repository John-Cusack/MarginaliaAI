"""The Rust audit seam must match `ClaimAuditService.audit`.

`normalize_audit_refs` + `first_missing_ref` cover the prelude and the
miss branch against fake repos: strip/dedupe/order, the verbatim empty
refusal, first-miss-wins. The non-string-refs divergence is pinned, not
papered over: Python raises `ValueError` where the seam answers
`TypeError` (the signature promises strings).

Deliberately out of scope: `slice_window` stays unseamed — it needs the
full document length, which `many_for_coordinates` never fetches (it
assembles `window_end` from the fetched slice instead). The validation
precedence tests below pin the assembly that stays Python: bad windows
and spans refuse before any fetch, on either backend.

Rust-forced cases skip when the accelerator wheel is absent; the Python
path always runs.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from research_engine.domain.errors import NotFoundError
from research_engine.services.argument import rules as rules_mod
from research_engine.services.argument.context import AnchorContextService
from research_engine.services.argument.rules import ClaimAuditService

DOC = UUID("12345678-1234-5678-1234-567812340000")


class _Texts:
    def __init__(self, texts: dict[UUID, str]) -> None:
        self._texts = texts
        self.calls: list = []

    async def get_spans(self, requests: list[tuple[UUID, int, int]]) -> list[str | None]:
        self.calls.append(requests)
        out = []
        for document_id, start, end in requests:
            text = self._texts.get(document_id)
            out.append(text[start:end] if text is not None else None)
        return out


class _Nodes:
    async def find_by_span(self, document_id: UUID, char_start: int, char_end: int) -> None:
        return None


class _Spans:
    async def get(self, span_id: UUID) -> None:
        raise NotFoundError("span", span_id)


def _service(text: str) -> tuple[AnchorContextService, _Texts]:
    texts = _Texts({DOC: text})
    return AnchorContextService(_Spans(), texts, _Nodes()), texts


class TestContextValidationPrecedence:
    @pytest.mark.parametrize(
        ("window", "coords"),
        [
            (True, [(DOC, 10, 20)]),
            (-1, [(DOC, 10, 20)]),
            (1_200, [(DOC, -1, 20)]),
            (1_200, [(DOC, 20, 20)]),
            (1_200, [(DOC, 30, 20)]),
        ],
    )
    def test_validation_precedes_fetch_on_both(self, window, coords, monkeypatch):
        """Bad windows and spans refuse before any fetch — the error
        precedence `many_for_coordinates` guarantees, backend-independent."""
        pytest.importorskip("marginalia_rs")
        import asyncio

        for backend in ("python", "rust"):
            monkeypatch.setenv("RE_RUST_BACKEND", backend)
            service, texts = _service("z" * 100)
            with pytest.raises(ValueError):
                asyncio.run(service.many_for_coordinates(coords, window=window))
            assert texts.calls == []

    def test_missing_text_raises_not_found(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        import asyncio

        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        service = AnchorContextService(_Spans(), _Texts({}), _Nodes())
        with pytest.raises(NotFoundError):
            asyncio.run(service.many_for_coordinates([(DOC, 10, 20)]))

    def test_assembly_offsets(self, monkeypatch):
        """The assembly that stays Python: request clamping plus
        fetched-slice length, no full-document length needed."""
        import asyncio

        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        service, _ = _service("w" * 500)
        (ctx,) = asyncio.run(service.many_for_coordinates([(DOC, 100, 110)]))
        assert (ctx.window_start, ctx.window_end) == (0, 500)
        assert ctx.quote_offset_in_window == 100
        assert ctx.quote_length == 10


class _Claims:
    def __init__(self, existing: set[str]) -> None:
        self._existing = existing
        self.seen: list[list[str] | None] = []

    async def existing_refs(self, refs: list[str]) -> set[str]:
        return self._existing

    async def audit(self, checked: list[str] | None) -> dict:
        self.seen.append(checked)
        return {"checked": checked}


class TestAuditRefsParity:
    @pytest.mark.parametrize(
        "refs",
        [
            None,
            [],
            ["C1"],
            ["  C2  ", "C1", "C2"],
        ],
    )
    def test_prelude_matches(self, refs, monkeypatch):
        pytest.importorskip("marginalia_rs")
        import asyncio

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        py_claims = _Claims({"C1", "C2"})
        py_out = asyncio.run(ClaimAuditService(py_claims).audit(refs))
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        rs_claims = _Claims({"C1", "C2"})
        rs_out = asyncio.run(ClaimAuditService(rs_claims).audit(refs))
        assert rs_out == py_out
        assert rs_claims.seen == py_claims.seen

    def test_empty_refs_refused_verbatim(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        import asyncio

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(ValueError) as py_exc:
            asyncio.run(ClaimAuditService(_Claims(set())).audit(["C1", "  "]))
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ValueError) as rs_exc:
            asyncio.run(ClaimAuditService(_Claims(set())).audit(["C1", "  "]))
        assert str(rs_exc.value) == str(py_exc.value)
        assert "non-empty claim refs" in str(rs_exc.value)

    def test_first_missing_wins(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        import asyncio

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(NotFoundError) as py_exc:
            asyncio.run(ClaimAuditService(_Claims({"C1"})).audit(["C1", "C9", "C8"]))
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(NotFoundError) as rs_exc:
            asyncio.run(ClaimAuditService(_Claims({"C1"})).audit(["C1", "C9", "C8"]))
        assert rs_exc.value.id == py_exc.value.id == "C9"

    def test_non_string_refs_pinned_boundary(self, monkeypatch):
        """`audit()` promises `Sequence[str]`: off-contract input raises
        `ValueError` on Python and `TypeError` on Rust. Pinned so the
        boundary cannot silently move."""
        pytest.importorskip("marginalia_rs")
        import asyncio

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(ValueError):
            asyncio.run(ClaimAuditService(_Claims(set())).audit(["C1", 42]))
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(TypeError):
            asyncio.run(ClaimAuditService(_Claims(set())).audit(["C1", 42]))

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import asyncio
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert rules_mod.ClaimAuditService is ClaimAuditService
        out = asyncio.run(ClaimAuditService(_Claims({"C1"})).audit([" C1 "]))
        assert out == {"checked": ["C1"]}
