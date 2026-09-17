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
        self.find_raw_calls: list = []

    async def lengths(self, document_id):
        if document_id not in self.raw:
            return None
        return len(self.raw[document_id]), len(self.norm[document_id])

    async def find_raw(self, document_id, needle):
        self.find_raw_calls.append((document_id, needle))
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

    @pytest.mark.asyncio
    async def test_em_dash_reports_normalized_never_exact(self):
        """A hyphen typed for the source's em dash is typography, not wording."""
        raw = {DOC: "He paused \u2014 and then continued."}
        result = await _verifier(raw).verify("He paused - and then continued.")

        assert result.tier is Tier.NORMALIZED
        assert result.tier is not Tier.EXACT
        assert result.verified
        assert result.location.source_text == "He paused \u2014 and then continued."


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
        assert result.documents_checked == 0
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
        # The source's continuation at the divergence, not the quote's own tail.
        assert result.divergence.source_continues == "Amos 5:24 makes it a flood."

    @pytest.mark.asyncio
    async def test_a_trivial_overlap_is_not_dressed_up_as_a_near_miss(self):
        """Below the threshold the "closest match" is a common phrase, and
        pointing at it would be worse than saying nothing."""
        result = await _verifier(near_threshold=0.9).verify(
            "The prophets pair two words but everything after this is invented"
        )

        assert result.tier is Tier.NOT_FOUND


class TestWindow:
    """The window hint: a caller quoting a hit already knows where it sits."""

    @pytest.mark.asyncio
    async def test_a_quote_from_a_hit_resolves_inside_its_window(self):
        texts = FakeTexts({DOC: SOURCE})
        verifier = QuoteVerifier(texts, FakePassages({DOC: SOURCE}), FakeDocuments())
        quote = SOURCE[20:100]

        result = await verifier.verify(quote, DOC, window=(20, 100))

        assert result.tier is Tier.EXACT
        assert (result.location.char_start, result.location.char_end) == (20, 100)
        # The whole-document search never ran.
        assert texts.find_raw_calls == []

    @pytest.mark.asyncio
    async def test_a_whitespace_differing_copy_returns_normalized(self):
        texts = FakeTexts({DOC: SOURCE})
        verifier = QuoteVerifier(texts, FakePassages({DOC: SOURCE}), FakeDocuments())
        quote = SOURCE[0:40].replace(" ", "  ")

        result = await verifier.verify(quote, DOC, window=(0, 80))

        assert result.tier is Tier.NORMALIZED
        assert result.verified

    @pytest.mark.asyncio
    async def test_a_quote_absent_from_the_window_falls_through(self):
        # Long tail so the slack around the window cannot reach the quote.
        raw = SOURCE + " Padding." * 500
        texts = FakeTexts({DOC: raw})
        verifier = QuoteVerifier(texts, FakePassages({DOC: raw}), FakeDocuments())
        quote = SOURCE[100:140]

        result = await verifier.verify(quote, DOC, window=(3000, 3100))

        assert result.tier is Tier.EXACT
        assert raw[result.location.char_start : result.location.char_end] == quote
        # The window missed, so the whole-document path ran after all.
        assert texts.find_raw_calls != []


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


class FakeNodes:
    """A structure tree over one document, answering `find_by_span`.

    Mirrors `PGDocumentNodeRepo`: the *deepest* node enclosing the span, and
    enclosing rather than overlapping, so a span crossing two siblings resolves
    to their parent.
    """

    def __init__(self, document_id, spans: list[tuple[str, int, int]]) -> None:
        self.document_id = document_id
        self.nodes = [
            SimpleNamespace(
                id=uuid.uuid4(), node_type="document", title="Whole",
                path="r", char_start=0, char_end=10_000, depth=0,
            )
        ] + [
            SimpleNamespace(
                id=uuid.uuid4(), node_type="verse", title=title,
                path=f"r.n{i}", char_start=start, char_end=end, depth=1,
            )
            for i, (title, start, end) in enumerate(spans)
        ]
        self.calls: list = []

    async def find_by_span(self, document_id, char_start, char_end):
        self.calls.append((document_id, char_start, char_end))
        if document_id != self.document_id:
            return None
        enclosing = [
            n for n in self.nodes
            if n.char_start <= char_start and char_end <= n.char_end
        ]
        return max(enclosing, key=lambda n: n.depth) if enclosing else None


class TestContainingNode:
    """What to cite, as opposed to which chunk it was retrieved from.

    A passage locator describes the chunker's window — for a versified text,
    several verses — so two quotations from the same verse can report different
    ranges purely because a chunk boundary fell between them. The structural
    node is the answer that does not move when the chunker changes.
    """

    def _verifier_with_nodes(self, spans):
        raw = {DOC: SOURCE}
        nodes = FakeNodes(DOC, spans)
        verifier = QuoteVerifier(
            FakeTexts(raw), FakePassages(raw), FakeDocuments(), nodes
        )
        return verifier, nodes

    @pytest.mark.asyncio
    async def test_a_quote_inside_one_node_reports_that_node(self):
        at = SOURCE.index("He requires")
        verifier, _ = self._verifier_with_nodes(
            [("Verse 1", 0, at), ("Verse 2", at, len(SOURCE))]
        )
        result = await verifier.verify("He requires")

        assert result.tier is Tier.EXACT
        assert result.location.node is not None
        assert result.location.node.title == "Verse 2"
        assert result.location.node.node_type == "verse"

    @pytest.mark.asyncio
    async def test_two_quotes_from_one_node_agree_across_a_chunk_boundary(self):
        """The defect this field exists to fix.

        `FakePassages` tiles at 60 characters, so these two quotations from the
        same node fall in different chunks and their passage locators disagree.
        The node must not.
        """
        at = SOURCE.index("He requires")
        verifier, _ = self._verifier_with_nodes(
            [("Verse 1", 0, at), ("Verse 2", at, len(SOURCE))]
        )
        head = await verifier.verify("He requires")
        tail = await verifier.verify("makes it a flood")

        assert head.location.locators != tail.location.locators
        assert head.location.node.title == tail.location.node.title == "Verse 2"

    @pytest.mark.asyncio
    async def test_a_quote_crossing_two_nodes_resolves_to_their_parent(self):
        """Naming either verse would be wrong, so it names neither."""
        at = SOURCE.index("He requires")
        verifier, _ = self._verifier_with_nodes(
            [("Verse 1", 0, at), ("Verse 2", at, len(SOURCE))]
        )
        result = await verifier.verify("two words. He requires")

        assert result.tier is Tier.EXACT
        assert result.location.node.node_type == "document"

    @pytest.mark.asyncio
    async def test_without_a_node_repository_the_field_is_simply_absent(self):
        """Every caller predating this constructs with three arguments."""
        result = await _verifier().verify("a phrase the translations render")

        assert result.tier is Tier.EXACT
        assert result.location.node is None

    @pytest.mark.asyncio
    async def test_a_structure_lookup_failure_does_not_fail_the_verification(self):
        """The tier and span are true whether or not the tree can be read."""

        class BrokenNodes:
            async def find_by_span(self, *_args):
                raise RuntimeError("ltree exploded")

        raw = {DOC: SOURCE}
        verifier = QuoteVerifier(
            FakeTexts(raw), FakePassages(raw), FakeDocuments(), BrokenNodes()
        )
        result = await verifier.verify("a phrase the translations render")

        assert result.tier is Tier.EXACT
        assert result.location.node is None

    @pytest.mark.asyncio
    async def test_a_near_miss_also_reports_where_it_landed(self):
        """Near misses resolve through the same path, so they get it too."""
        at = SOURCE.index("He requires")
        verifier, _ = self._verifier_with_nodes(
            [("Verse 1", 0, at), ("Verse 2", at, len(SOURCE))]
        )
        result = await verifier.verify(
            "a phrase the translations render badly and never well"
        )

        assert result.tier is Tier.NEAR
        assert result.location.node is not None
