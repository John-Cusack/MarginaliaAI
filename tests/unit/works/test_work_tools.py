"""Tool envelopes: unconfigured, bad input, and missing files answer, not crash."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from research_engine.mcp.tools import (
    verify_quote,
    work_citations,
    work_cite_entry,
    work_render,
    work_verify,
)
from research_engine.services.verification.quote import Tier
from research_engine.services.works.cite import CitationResult, QuoteUnverifiedError
from research_engine.services.works.files import WorkFileReader

pytestmark = pytest.mark.unit


def _bare() -> SimpleNamespace:
    return SimpleNamespace(
        work_verifier=None,
        work_renderer=None,
        work_files=None,
        works_mirror_available=False,
    )


class TestWorkVerifyTool:
    @pytest.mark.asyncio
    async def test_unconfigured(self):
        result = await work_verify.handler(_bare())

        assert result["error"]["code"] == "works_not_configured"

    @pytest.mark.asyncio
    async def test_bad_gate(self, tmp_path):
        container = SimpleNamespace(
            work_verifier=object(), work_files=WorkFileReader(tmp_path)
        )

        result = await work_verify.handler(container, gate="someday")

        assert result["error"]["code"] == "validation_error"

    @pytest.mark.asyncio
    async def test_missing_file(self, tmp_path):
        container = SimpleNamespace(
            work_verifier=object(), work_files=WorkFileReader(tmp_path)
        )

        result = await work_verify.handler(container, path="absent.md")

        assert result["error"]["code"] == "not_found"


class TestWorkCitationsTool:
    @pytest.mark.asyncio
    async def test_unconfigured(self):
        result = await work_citations.handler(_bare(), document_id="x")

        assert result["error"]["code"] == "works_not_configured"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"document_id": "d", "zotero_key": "z"},
            {"document_id": "d", "claim_ref": "c"},
            {"document_id": "d", "zotero_key": "z", "claim_ref": "c"},
        ],
    )
    async def test_exactly_one_selector(self, tmp_path, kwargs):
        container = SimpleNamespace(work_files=WorkFileReader(tmp_path))

        result = await work_citations.handler(container, **kwargs)

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_bad_uuid(self, tmp_path):
        container = SimpleNamespace(work_files=WorkFileReader(tmp_path))

        result = await work_citations.handler(container, document_id="not-a-uuid")

        assert result["error"]["code"] == "invalid_input"


class TestWorkRenderTool:
    @pytest.mark.asyncio
    async def test_unconfigured(self):
        result = await work_render.handler(_bare(), path="essay.md")

        assert result["error"]["code"] == "works_not_configured"

    @pytest.mark.asyncio
    async def test_missing_file(self, tmp_path):
        container = SimpleNamespace(
            work_renderer=object(), work_files=WorkFileReader(tmp_path)
        )

        result = await work_render.handler(container, path="absent.md")

        assert result["error"]["code"] == "not_found"


class FakeVerification:
    def __init__(self) -> None:
        self.seen = None

    async def verify(self, text, document_id=None, *, window=None):
        self.seen = (text, document_id, window)
        return SimpleNamespace(
            tier=Tier.EXACT,
            verified=True,
            detail="exact",
            documents_checked=1,
            location=None,
            matched_fraction=None,
            divergence=None,
        )


class FakeCiter:
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls: list = []

    async def cite(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class TestWorkCiteEntryTool:
    def _result(self):
        return CitationResult(
            entry={"document_id": "d", "char_start": 1, "char_end": 2,
                   "quoted_text": "q", "intent": "quotation"},
            entry_yaml="char_start: 1\n",
            tier="exact",
            verified_span=[1, 2],
            span_id="s",
        )

    @pytest.mark.asyncio
    async def test_bad_uuid(self):
        container = SimpleNamespace(work_citer=FakeCiter(result=self._result()))

        result = await work_cite_entry.handler(
            container, document_id="nope", quoted_text="q", intent="quotation"
        )

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_bad_window(self):
        container = SimpleNamespace(work_citer=FakeCiter(result=self._result()))

        result = await work_cite_entry.handler(
            container,
            document_id="11111111-1111-1111-1111-111111111111",
            quoted_text="q",
            intent="quotation",
            window={"char_start": 9, "char_end": 9},
        )

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_service_value_error_is_invalid_input(self):
        container = SimpleNamespace(work_citer=FakeCiter(error=ValueError("bad intent")))

        result = await work_cite_entry.handler(
            container,
            document_id="11111111-1111-1111-1111-111111111111",
            quoted_text="q",
            intent="frobnicate",
        )

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_unverified_quote_refuses_with_its_tier(self):
        error = QuoteUnverifiedError(
            tier=Tier.NEAR, detail="Quote verifies near", divergence=None
        )
        container = SimpleNamespace(work_citer=FakeCiter(error=error))

        result = await work_cite_entry.handler(
            container,
            document_id="11111111-1111-1111-1111-111111111111",
            quoted_text="changed",
            intent="quotation",
        )

        assert result["error"]["code"] == "quote_unverified"
        assert result["error"]["details"]["tier"] == "near"

    @pytest.mark.asyncio
    async def test_happy_path_returns_the_entry(self):
        citer = FakeCiter(result=self._result())
        container = SimpleNamespace(work_citer=citer)

        result = await work_cite_entry.handler(
            container,
            document_id="11111111-1111-1111-1111-111111111111",
            quoted_text="q",
            intent="quotation",
            window={"char_start": 0, "char_end": 5},
        )

        assert result["tier"] == "exact"
        assert result["entry"]["char_start"] == 1
        assert citer.calls[0]["window"] == (0, 5)


class TestVerifyQuoteWindow:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "window",
        [
            {"char_start": 10},  # missing char_end
            {"char_start": 20, "char_end": 10},  # end before start
            {"char_start": -1, "char_end": 10},  # negative start
            {"char_start": True, "char_end": 10},  # bool is not an offset
            [10, 20],  # not an object
        ],
    )
    async def test_bad_windows_are_invalid_input(self, window):
        container = SimpleNamespace(verification=FakeVerification())

        result = await verify_quote.handler(container, text="hi", window=window)

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_good_window_reaches_the_service_as_a_tuple(self):
        verification = FakeVerification()
        container = SimpleNamespace(verification=verification)

        result = await verify_quote.handler(
            container, text="hi", window={"char_start": 10, "char_end": 20}
        )

        assert result["tier"] == "exact"
        assert verification.seen[2] == (10, 20)
