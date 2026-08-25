"""Checking a quotation against what the source actually says.

The invariant this suite exists to defend is the tier boundary. A quotation that
only matches after typographic folding must report `normalized`, never `exact`
— a researcher deciding whether to put something inside quotation marks is
relying on that distinction, and collapsing the two would give a confident wrong
answer rather than an honest hedged one.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from research_engine.services.text.normalize import normalize
from research_engine.services.verification import QuoteVerifier, Tier

DOC = uuid.uuid4()
EMPTY_DOC = uuid.uuid4()

SOURCE = (
    "The prophets pair two words. He requires “justice and righteousness” of "
    "every ruler, a phrase the translations render un-\nevenly, and Amos 5:24 "
    "makes it a flood."
)


class FakeTexts:
    """An in-memory `document_texts`, normalized exactly as the column is."""

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.norm = {k: normalize(v) for k, v in raw.items()}

    async def lengths(self, document_id):
        if document_id not in self.raw:
            return None
        return len(self.raw[document_id]), len(self.norm[document_id])

    async def find_raw(self, document_id, needle):
        at = self.raw.get(document_id, "").find(needle)
        return at if at >= 0 and needle else None

    async def find_normalized(self, document_id, needle):
        if not needle:
            return None
        at = self.norm.get(document_id, "").find(needle)
        return at if at >= 0 else None

    async def get_span(self, document_id, start, end):
        if document_id not in self.raw:
            return None
        return self.raw[document_id][start:end]

    async def get_text(self, document_id):
        return self.raw.get(document_id)

    async def find_documents_containing(self, needle, limit=10):
        if not needle:
            return []
        return [k for k, v in self.norm.items() if needle in v][:limit]


class FakePassages:
    """Passages tiling each document in fixed-width chunks."""

    def __init__(self, raw: dict, width: int = 60) -> None:
        self.spans = {}
        for doc, text in raw.items():
            self.spans[doc] = [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    char_start=i,
                    char_end=min(i + width, len(text)),
                    locator={"page": i // width + 1},
                )
                for i in range(0, max(len(text), 1), width)
            ]

    async def covering_span(self, document_id, char_start, char_end):
        return [
            p
            for p in self.spans.get(document_id, [])
            if p.char_start < char_end and p.char_end > char_start
        ]


class FakeDocuments:
    async def get(self, document_id):
        return SimpleNamespace(title="The Prophets on Justice")


def _verifier(raw=None, **kw):
    raw = {DOC: SOURCE} if raw is None else raw
    return QuoteVerifier(FakeTexts(raw), FakePassages(raw), FakeDocuments(), **kw)


class TestTiers:
    @pytest.mark.asyncio
    async def test_exact_match_reports_exact_and_the_right_span(self):
        result = await _verifier().verify("a phrase the translations render")

        assert result.tier is Tier.EXACT
        assert result.verified
        assert SOURCE[result.location.char_start : result.location.char_end] == (
            "a phrase the translations render"
        )

    @pytest.mark.asyncio
    async def test_typography_reports_normalized_never_exact(self):
        """The invariant. Straight quotes against a source with curly ones."""
        result = await _verifier().verify('"justice and righteousness"')

        assert result.tier is Tier.NORMALIZED
        assert result.tier is not Tier.EXACT
        assert result.verified

    @pytest.mark.asyncio
    async def test_a_normalized_hit_returns_the_untouched_source_typography(self):
        """What the caller needs in order to quote it correctly after all."""
        result = await _verifier().verify('"justice and righteousness"')

        assert result.location.source_text == "“justice and righteousness”"

    @pytest.mark.asyncio
    async def test_line_break_hyphenation_is_folded_and_still_located(self):
        result = await _verifier().verify("render unevenly")

        assert result.tier is Tier.NORMALIZED
        assert result.location.source_text == "render un-\nevenly"

    @pytest.mark.asyncio
    async def test_collapsed_whitespace_alone_is_still_not_exact(self):
        result = await _verifier().verify("The prophets   pair\n\ntwo words.")

        assert result.tier is Tier.NORMALIZED


class TestChunkStraddling:
    @pytest.mark.asyncio
    async def test_a_quote_crossing_a_boundary_reports_every_passage(self):
        """The reason this searches document text rather than passage text."""
        # Spans characters 20..100 against 60-character chunks.
        quote = SOURCE[20:100]
        result = await _verifier().verify(quote)

        assert result.tier is Tier.EXACT
        assert len(result.location.passage_ids) > 1
        assert result.location.straddles_passages
        assert len(result.location.locators) > 1


class TestHonestAbsence:
    @pytest.mark.asyncio
    async def test_a_document_with_no_text_is_not_reported_as_not_found(self):
        """"Cannot check" and "is not there" are different answers.

        Conflating them teaches a researcher to distrust a tool that was never
        given anything to read.
        """
        verifier = _verifier({DOC: SOURCE})
        result = await verifier.verify("anything at all", document_id=EMPTY_DOC)

        assert result.tier is Tier.NO_CANONICAL_TEXT
        assert not result.verified
        assert "not the same as" in result.detail

    @pytest.mark.asyncio
    async def test_absent_text_is_not_found(self):
        result = await _verifier().verify(
            "the quick brown fox jumped over the lazy dog entirely"
        )

        assert result.tier is Tier.NOT_FOUND
        assert not result.verified

    @pytest.mark.asyncio
    async def test_an_empty_quote_is_refused_rather_than_matching_everything(self):
        result = await _verifier().verify("   ")

        assert result.tier is Tier.NOT_FOUND


class TestNearMiss:
    @pytest.mark.asyncio
    async def test_a_wrong_ending_reports_where_it_diverges(self):
        result = await _verifier().verify(
            "of every ruler, a phrase the translations render unevenly, and the moon"
        )

        assert result.tier is Tier.NEAR
        assert not result.verified
        assert 0.5 <= result.matched_fraction < 1.0
        assert "moon" in result.divergence.quote_continues
        assert result.divergence.matched_characters > 0

    @pytest.mark.asyncio
    async def test_a_trivial_overlap_is_not_dressed_up_as_a_near_miss(self):
        """Below the threshold the "closest match" is a common phrase, and
        pointing at it would be worse than saying nothing."""
        result = await _verifier(near_threshold=0.9).verify(
            "The prophets pair two words but everything after this is invented"
        )

        assert result.tier is Tier.NOT_FOUND


class TestWindowing:
    @pytest.mark.asyncio
    async def test_a_whitespace_heavy_document_still_resolves_exactly(self):
        """The windowed lookup estimates from the raw:normalized ratio.

        A document that is mostly whitespace breaks that estimate badly — the
        match sits at raw offset ~200k while its normalized offset is ~20. This
        is the case the widening steps and the whole-document fallback exist
        for, and it must produce the same exact span, not an approximate one.
        """
        raw = "opening. " + " " * 200_000 + "the buried sentence follows here."
        doc = uuid.uuid4()
        result = await _verifier({doc: raw}).verify("the buried  sentence follows")

        assert result.tier is Tier.NORMALIZED
        start, end = result.location.char_start, result.location.char_end
        assert raw[start:end] == "the buried sentence follows"
