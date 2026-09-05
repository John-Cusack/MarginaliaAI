"""Tool envelopes: unconfigured, bad input, and missing files answer, not crash."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from research_engine.mcp.tools import verify_quote, work_citations, work_render, work_verify
from research_engine.services.verification.quote import Tier
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
