"""Making citations: verified quotes become entries; the rest refuse cleanly."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import yaml

from research_engine.services.verification.quote import Tier
from research_engine.services.works.cite import QuoteUnverifiedError, WorkCiter

pytestmark = pytest.mark.unit

DOC = uuid.uuid4()


class FakeVerification:
    def __init__(
        self,
        tier: Tier = Tier.EXACT,
        span: tuple[int, int] | None = (34, 62),
    ) -> None:
        self.tier = tier
        self.span = span
        self.calls: list = []

    async def verify(self, quote, document_id=None, *, window=None):
        self.calls.append((quote, document_id, window))
        location = None
        if self.span is not None:
            location = SimpleNamespace(char_start=self.span[0], char_end=self.span[1])
        return SimpleNamespace(
            tier=self.tier, location=location, divergence=None, matched_fraction=None
        )


class FakeSpans:
    """The resolver: records calls, hands back a stable row id."""

    def __init__(self) -> None:
        self.calls: list = []
        self.row_id = uuid.uuid4()

    async def resolve(self, tx, *, document_id, char_start, char_end):
        self.calls.append((document_id, char_start, char_end))
        return SimpleNamespace(id=self.row_id)


class FakeEngine:
    @asynccontextmanager
    async def begin(self):
        yield object()


def _citer(**fakes) -> tuple[WorkCiter, FakeVerification, FakeSpans]:
    verification = fakes.pop("verification", FakeVerification())
    spans = fakes.pop("spans", FakeSpans())
    return WorkCiter(verification, spans, engine=FakeEngine()), verification, spans


class TestCite:
    @pytest.mark.asyncio
    async def test_exact_quote_becomes_a_paste_ready_entry(self):
        citer, verification, spans = _citer()

        result = await citer.cite(
            document_id=DOC,
            quoted_text="The prophets pair two words.",
            intent="quotation",
            citation_id="c1",
            zotero_key="DABAR_2026",
            locator={"page": 1},
        )

        assert result.tier == "exact"
        assert result.verified_span == [34, 62]
        assert result.span_id == str(spans.row_id)
        assert result.entry == {
            "id": "c1",
            "intent": "quotation",
            "document_id": str(DOC),
            "char_start": 34,
            "char_end": 62,
            "quoted_text": "The prophets pair two words.",
            "zotero_key": "DABAR_2026",
            "locator": {"page": 1},
        }
        # The entry parses back as YAML and the resolver saw verified offsets.
        assert yaml.safe_load(result.entry_yaml) == result.entry
        assert spans.calls == [(DOC, 34, 62)]

    @pytest.mark.asyncio
    async def test_normalized_quote_keeps_the_typed_text_at_verified_offsets(self):
        citer, _, _ = _citer(
            verification=FakeVerification(tier=Tier.NORMALIZED, span=(63, 117))
        )

        result = await citer.cite(
            document_id=DOC,
            quoted_text='He requires "justice and righteousness" of every ruler',
            intent="quotation",
        )

        assert result.tier == "normalized"
        assert result.entry["char_start"] == 63
        assert result.entry["char_end"] == 117
        assert result.entry["quoted_text"].startswith("He requires")
        assert "id" not in result.entry

    @pytest.mark.asyncio
    async def test_window_reaches_the_verifier(self):
        citer, verification, _ = _citer()

        await citer.cite(
            document_id=DOC,
            quoted_text="a phrase",
            intent="background",
            window=(100, 160),
        )

        assert verification.calls[0][2] == (100, 160)

    @pytest.mark.asyncio
    async def test_near_quote_refuses_and_resolves_nothing(self):
        citer, _, spans = _citer(verification=FakeVerification(tier=Tier.NEAR))

        with pytest.raises(QuoteUnverifiedError) as exc_info:
            await citer.cite(
                document_id=DOC, quoted_text="changed words here", intent="quotation"
            )

        assert exc_info.value.tier is Tier.NEAR
        assert spans.calls == []

    @pytest.mark.asyncio
    async def test_not_found_refuses_and_resolves_nothing(self):
        citer, _, spans = _citer(verification=FakeVerification(tier=Tier.NOT_FOUND))

        with pytest.raises(QuoteUnverifiedError):
            await citer.cite(
                document_id=DOC, quoted_text="never written", intent="quotation"
            )

        assert spans.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fields",
        [
            {"intent": "frobnicate"},
            {"role": "cheerleader"},
            {"citation_id": "x1"},
            {"locator": ["page", 1]},
        ],
    )
    async def test_malformed_fields_fail_before_any_check(self, fields):
        citer, verification, spans = _citer()
        kwargs = {
            "document_id": DOC,
            "quoted_text": "The prophets pair two words.",
            "intent": "quotation",
            **fields,
        }

        with pytest.raises(ValueError):
            await citer.cite(**kwargs)  # type: ignore[arg-type]

        assert verification.calls == []
        assert spans.calls == []
