"""Choosing how much to read around a hit.

The two cases that matter are the extremes measured on the real corpus, and a
rule that handles one naturally gets the other wrong. Louw-Nida's median node is
68 characters — narrower than the chunk that matched it — so "read the containing
node" returns *less* than search already showed. A Marginal Jew's chapters run to
24,000 characters and the root node is the whole 23.2M-character document, so
"read the containing node" returns everything.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from research_engine.domain.nodes import DocumentNode
from research_engine.services.search.windows import (
    PassageWindowReader,
    choose_window,
)
from research_engine.services.text.anchoring import Span

DOC = uuid.uuid4()


def node(start: int, end: int, depth: int, title: str) -> DocumentNode:
    return DocumentNode(
        id=uuid.uuid4(),
        document_id=DOC,
        parent_id=None,
        path="r" + ".n0" * depth,
        depth=depth,
        position=0,
        node_type="section",
        title=title,
        char_start=start,
        char_end=end,
        created_at=datetime.now(UTC),
    )


class TestLouwNida:
    """The deepest node is narrower than the chunk that matched it."""

    ROOT = node(0, 200_000, 0, "Louw-Nida")
    DOMAIN = node(1_000, 1_400, 1, "Domain 56: Justice")
    ENTRY = node(1_200, 1_268, 2, "56.29 κρίσις")
    CHAIN = [ROOT, DOMAIN, ENTRY]
    PASSAGE = Span(1_190, 1_290)  # deliberately wider than its own node

    def test_the_window_is_never_narrower_than_the_chunk(self):
        plan = choose_window(
            self.PASSAGE, self.CHAIN, budget_chars=6_000, min_chars=800
        )
        assert plan.span.start <= self.PASSAGE.start
        assert plan.span.end >= self.PASSAGE.end

    def test_it_climbs_past_a_parent_too_small_to_be_worth_reading(self):
        """The point of the minimum.

        The 400-character domain node is the widest that fits the budget, and
        returning it would give a reader barely more than the chunk. Reaching a
        useful amount of context necessarily means leaving it — a 400-character
        box cannot hold 800 characters, and pretending otherwise would make the
        feature inert on the corpus that needs it most.
        """
        plan = choose_window(
            self.PASSAGE, self.CHAIN, budget_chars=6_000, min_chars=800
        )
        assert plan.span.width >= 800
        assert plan.source == "node_window"
        # It climbed to the root, the only ancestor that can hold 800 chars.
        assert plan.node is self.ROOT

    def test_it_stays_inside_the_ancestor_it_climbed_to(self):
        plan = choose_window(
            self.PASSAGE, self.CHAIN, budget_chars=6_000, min_chars=800
        )
        assert plan.span.start >= self.ROOT.char_start
        assert plan.span.end <= self.ROOT.char_end

    def test_a_node_that_is_already_big_enough_is_returned_whole(self):
        """With a lower minimum the domain node itself is the right answer."""
        plan = choose_window(
            self.PASSAGE, self.CHAIN, budget_chars=6_000, min_chars=200
        )
        assert plan.source == "node"
        assert plan.node is self.DOMAIN
        assert (plan.span.start, plan.span.end) == (1_000, 1_400)


class TestMarginalJew:
    """Every containing node exceeds the budget."""

    ROOT = node(0, 5_000_000, 0, "A Marginal Jew")
    PART = node(0, 900_000, 1, "Part Two")
    CHAPTER = node(100_000, 124_267, 2, "Chapter 14")
    CHAIN = [ROOT, PART, CHAPTER]
    PASSAGE = Span(110_000, 112_000)

    def test_the_window_is_capped_at_the_budget(self):
        plan = choose_window(
            self.PASSAGE, self.CHAIN, budget_chars=6_000, min_chars=800
        )
        assert plan.span.width <= 6_000
        assert plan.source == "node_window"

    def test_it_does_not_bleed_into_the_previous_chapter(self):
        """Why the fallback clips to the narrowest node rather than floating."""
        plan = choose_window(
            self.PASSAGE, self.CHAIN, budget_chars=6_000, min_chars=800
        )
        assert plan.span.start >= self.CHAPTER.char_start
        assert plan.span.end <= self.CHAPTER.char_end

    def test_a_hit_at_the_very_start_of_a_chapter_still_gets_a_full_window(self):
        """Slide the window rather than truncating it against the boundary."""
        plan = choose_window(
            Span(100_010, 100_100), self.CHAIN, budget_chars=6_000, min_chars=800
        )
        assert plan.span.width == 6_000
        assert plan.span.start == self.CHAPTER.char_start


class TestDegenerateTrees:
    def test_a_root_only_tree_does_not_return_the_whole_document(self):
        root = node(0, 23_198_553, 0, "TDNT")
        plan = choose_window(
            Span(500_000, 501_000), [root], budget_chars=6_000, min_chars=800
        )
        assert plan.span.width <= 6_000
        assert plan.span.width < 23_198_553

    def test_no_ancestors_at_all_still_produces_a_window(self):
        plan = choose_window(
            Span(5_000, 5_200), [], budget_chars=6_000, min_chars=800
        )
        assert plan.source == "document_window"
        assert plan.span.start >= 0
        assert plan.span.width >= 800

    def test_a_window_near_offset_zero_is_not_negative(self):
        plan = choose_window(
            Span(10, 60), [], budget_chars=6_000, min_chars=800
        )
        assert plan.span.start == 0

    def test_a_budget_below_the_chunk_width_returns_the_chunk(self):
        """The floor. Never hand back less than the caller already had."""
        passage = Span(1_000, 9_000)
        plan = choose_window(
            passage, [node(0, 50_000, 0, "doc")], budget_chars=500, min_chars=200
        )
        assert (plan.span.start, plan.span.end) == (passage.start, passage.end)
        assert plan.source == "passage"

    def test_a_passage_without_offsets_has_no_window(self):
        assert choose_window(None, [], budget_chars=6_000, min_chars=800) is None


@pytest.mark.parametrize("budget", [800, 6_000, 60_000])
def test_the_window_always_contains_the_passage(budget):
    """The one property that must hold whatever the tree looks like."""
    chain = [
        node(0, 1_000_000, 0, "root"),
        node(9_000, 40_000, 1, "part"),
        node(9_500, 9_700, 2, "leaf"),
    ]
    passage = Span(9_400, 9_800)

    plan = choose_window(passage, chain, budget_chars=budget, min_chars=800)

    assert plan.span.start <= passage.start
    assert plan.span.end >= passage.end


def test_a_node_identical_to_the_chunk_is_not_treated_as_a_window():
    """Median passages-per-node in this corpus is 1.

    So the deepest node is routinely the chunk itself. It clears any minimum
    while expanding nothing, and returning it would report a window that is not
    one — which is what happened on a third of lexicon hits before the rule
    required a window to be strictly wider than what matched.
    """
    passage = Span(1_000, 1_704)
    chain = [
        node(0, 400_000, 0, "lexicon"),
        node(500, 3_000, 1, "56 Courts"),
        node(1_000, 1_704, 2, "56.29"),  # exactly the passage
    ]

    plan = choose_window(passage, chain, budget_chars=2_700, min_chars=366)

    assert plan.span.width > passage.width
    assert plan.source != "passage"


class FakeNodes:
    def __init__(self, chains: dict) -> None:
        self.chains = chains
        self.calls = 0

    async def get_ancestors_many(self, node_ids):
        self.calls += 1
        return {n: self.chains.get(n, []) for n in node_ids}


class FakeTexts:
    def __init__(self, text: str, *, missing: bool = False) -> None:
        self.text = text
        self.missing = missing
        self.calls = 0

    async def get_spans(self, requests):
        self.calls += 1
        if self.missing:
            return [None] * len(requests)
        return [self.text[s:e] for _, s, e in requests]


def passage(pid, start, end, node_id, text):
    return SimpleNamespace(
        id=pid, document_id=DOC, char_start=start, char_end=end,
        node_id=node_id, text=text,
    )


class TestReaderBatching:
    @pytest.mark.asyncio
    async def test_twenty_hits_cost_two_queries_not_forty(self):
        """The property that keeps expansion from reintroducing the N+1.

        The read path had just been taken from 50 single-row queries down to
        one; expanding every hit is exactly the kind of change that quietly puts
        them back. Nothing about the returned windows would look wrong if it did.
        """
        leaf = node(0, 4_000, 1, "section")
        nodes = FakeNodes({leaf.id: [node(0, 100_000, 0, "doc"), leaf]})
        texts = FakeTexts("x" * 100_000)
        reader = PassageWindowReader(nodes, texts, max_tokens=500, min_tokens=100)

        hits = [
            passage(uuid.uuid4(), i * 200, i * 200 + 120, leaf.id, "chunk " * 20)
            for i in range(20)
        ]
        windows = await reader.read(hits)

        assert len(windows) == 20
        assert nodes.calls == 1
        assert texts.calls == 1

    @pytest.mark.asyncio
    async def test_a_document_with_no_text_yields_no_window_rather_than_an_error(self):
        leaf = node(0, 4_000, 1, "section")
        reader = PassageWindowReader(
            FakeNodes({leaf.id: [leaf]}),
            FakeTexts("", missing=True),
            max_tokens=500,
            min_tokens=100,
        )

        windows = await reader.read(
            [passage(uuid.uuid4(), 0, 100, leaf.id, "chunk " * 20)]
        )

        assert windows == {}

    @pytest.mark.asyncio
    async def test_a_passage_without_offsets_is_skipped_not_guessed_at(self):
        reader = PassageWindowReader(
            FakeNodes({}), FakeTexts("x" * 1_000), max_tokens=500, min_tokens=100
        )

        windows = await reader.read(
            [passage(uuid.uuid4(), None, None, None, "chunk")]
        )

        assert windows == {}

    @pytest.mark.asyncio
    async def test_the_window_text_matches_the_span_it_claims(self):
        """The same invariant `PassageDraft` keeps, on the way back out."""
        canonical = "".join(f"sentence {i}. " for i in range(500))
        leaf = node(0, len(canonical), 1, "section")
        texts = FakeTexts(canonical)
        reader = PassageWindowReader(
            FakeNodes({leaf.id: [leaf]}), texts, max_tokens=200, min_tokens=50
        )
        pid = uuid.uuid4()

        windows = await reader.read(
            [passage(pid, 1_000, 1_100, leaf.id, canonical[1_000:1_100])]
        )

        window = windows[pid]
        assert window.text == canonical[window.char_start : window.char_end]
