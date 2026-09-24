"""Choosing how much of a document to read around a search hit.

A chunk is the right unit to embed and rank and the wrong unit to read: it ends
where the ingester happened to cut, which in a lexicon lands mid-definition and
in a monograph mid-argument. Retrieval still ranks chunks; this decides what
prose comes back beside each one.

The structural tree is the natural boundary — a lexicon entry, a section — and
on its own it is not enough, because node spans are wildly uneven. Measured on
this corpus (p50 / p90 characters):

    Louw-Nida        68 /    553
    HALOT           290 /  1,554
    BDAG            461 /  2,206
    TDNT          1,676 /  7,441
    A Marginal Jew 4,813 / 24,267

Median passages-per-node is 1, and `build_node_tree` widens parents to cover
their children, so along a root->node chain the spans nest and the widths only
shrink. Two consequences drive everything here:

* The deepest node is often **narrower than the chunk that matched it**. Reading
  "the containing node" for Louw-Nida would hand back less than search already
  showed you. So a minimum matters as much as a maximum, and reaching it means
  climbing to an ancestor that can actually hold it — a 400-character parent
  cannot, and staying inside it would defeat the point.
* The widest node is always the root, i.e. the whole document, up to 23.2M
  characters. So the budget is never optional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from research_engine.domain.passages import PassageWindow
from research_engine.services.ingestion.chunking.fixed_window import trim_span
from research_engine.services.text.anchoring import Span
from research_engine.services.text.tokens import (
    approx_tokens,
    chars_per_token,
    token_budget_chars,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from research_engine.domain.nodes import DocumentNode
    from research_engine.domain.passages import Passage

WindowSource = Literal["node", "node_window", "document_window", "passage"]


class WindowPlan(NamedTuple):
    """Where to read, and how that was decided."""

    span: Span
    source: WindowSource
    #: The node that bounded the window — not always the passage's own node.
    node: DocumentNode | None


def choose_window(
    passage: Span | None,
    ancestors: Sequence[DocumentNode],
    *,
    budget_chars: int,
    min_chars: int,
) -> WindowPlan | None:
    """Decide the span to read around *passage*.

    *ancestors* is the chain from `get_ancestors`, in any order — it is sorted
    here by width rather than trusted, so a malformed tree degrades to a
    sensible window instead of an incoherent one.

    Returns None when there is no passage span to build around. A passage
    without offsets predates the requirement and cannot be located in the
    canonical text at all.
    """
    if passage is None:
        return None

    # Widest first. The documented invariant is that parents enclose children,
    # which makes this the same as root-first; sorting means we do not depend on
    # it holding for every tree ever written.
    chain = sorted(ancestors, key=_width, reverse=True)
    fits = [node for node in chain if _width(node) <= budget_chars]

    if not fits:
        # Every node is too big — the common case for TDNT articles and for any
        # passage that straddles two sections and so resolved to the root.
        # Clip to the narrowest node rather than floating free, so a hit near the
        # start of chapter 14 does not read backwards into chapter 13.
        bound = chain[-1] if chain else None
        return _plan(_centred(passage, budget_chars, bound), bound, passage)

    # A window has to clear the minimum *and* actually contain more than the
    # chunk. Median passages-per-node here is 1, so the deepest node frequently
    # is the chunk: it clears any minimum and expands nothing. Measured on the
    # live corpus, that silently returned `source="passage"` at 1.0x for a third
    # of lexicon hits before this was a condition.
    worth_reading = max(min_chars, passage.width + 1)

    chosen = fits[0]
    if _width(chosen) >= worth_reading:
        return _plan(Span(chosen.char_start, chosen.char_end), chosen, passage)

    # The widest node that fits adds too little to be worth reading. Climb to
    # the smallest ancestor that can hold a useful window and widen inside it.
    # Staying within `chosen` would be structurally tidy and useless: for
    # Louw-Nida it returns a 68-character entry.
    # Read to the budget, not merely to the threshold. Structure has failed to
    # give a useful boundary, so the token budget is the only meaningful limit
    # left — and it is the knob the operator actually set. Targeting the
    # threshold instead produced windows one character wider than the chunk.
    bound = next((node for node in chain if _width(node) >= worth_reading), None)
    return _plan(_centred(passage, budget_chars, bound), bound, passage)


def _width(node: DocumentNode) -> int:
    return node.char_end - node.char_start


def _centred(passage: Span, width: int, bound: DocumentNode | None) -> Span:
    """A span of *width* centred on *passage*, clipped to *bound*."""
    lo = bound.char_start if bound else 0
    hi = bound.char_end if bound else None

    midpoint = (passage.start + passage.end) // 2
    start = midpoint - width // 2
    end = start + width

    # Slide rather than truncate when the window runs off one end, so a hit near
    # the start of a section still gets a full window's worth of context.
    if start < lo:
        start, end = lo, lo + width
    if hi is not None and end > hi:
        end = hi
        start = max(lo, hi - width)
    return Span(max(start, 0), max(end, 0))


def _plan(
    span: Span, bound: DocumentNode | None, passage: Span
) -> WindowPlan:
    """Apply the floor and name what happened.

    The floor is unconditional and last: whatever the structure says, never hand
    back less than the chunk the caller already had.
    """
    span = Span(min(span.start, passage.start), max(span.end, passage.end))
    if span == passage:
        return WindowPlan(span, "passage", bound)
    if bound is None:
        return WindowPlan(span, "document_window", None)
    if span.start == bound.char_start and span.end == bound.char_end:
        return WindowPlan(span, "node", bound)
    return WindowPlan(span, "node_window", bound)


class PassageWindowReader:
    """Reads the expanded window for a batch of hits.

    Two queries for the whole batch regardless of `k`: one for the ancestor
    chains, one for the slices. Search expands every hit, so a per-hit form here
    would put the N+1 straight back into a read path that just lost it.
    """

    def __init__(
        self,
        document_nodes: Any,
        document_texts: Any,
        *,
        max_tokens: int,
        min_tokens: int,
    ) -> None:
        self._nodes = document_nodes
        self._texts = document_texts
        self._max_tokens = max_tokens
        self._min_tokens = min_tokens

    async def read(self, passages: Sequence[Passage]) -> dict[UUID, PassageWindow]:
        if not passages:
            return {}

        node_ids = list({p.node_id for p in passages if p.node_id is not None})
        chains = await self._nodes.get_ancestors_many(node_ids) if node_ids else {}

        planned: list[tuple[Passage, WindowPlan, list[DocumentNode]]] = []
        for passage in passages:
            span = _passage_span(passage)
            chain = chains.get(passage.node_id, []) if passage.node_id else []
            # Size the budget from the hit's own text: it is in hand, free, and
            # it is the local script mix. The same token budget is a much
            # shorter character window in Greek or Hebrew than in English, and
            # `token_budget_chars` is what knows that.
            rate = chars_per_token(passage.text) if passage.text else None
            plan = choose_window(
                span,
                chain,
                budget_chars=token_budget_chars(self._max_tokens, rate)
                if rate
                else self._max_tokens * 4,
                min_chars=token_budget_chars(self._min_tokens, rate)
                if rate
                else self._min_tokens * 4,
            )
            if plan is not None:
                planned.append((passage, plan, chain))

        if not planned:
            return {}

        requests = [
            (p.document_id, plan.span.start, plan.span.end) for p, plan, _ in planned
        ]
        slices = await self._texts.get_spans(requests)

        windows: dict[UUID, PassageWindow] = {}
        for (passage, plan, chain), raw in zip(planned, slices, strict=True):
            window = _build_window(passage, plan, chain, raw)
            if window is not None:
                windows[passage.id] = window
        return windows


def _passage_span(passage: Passage) -> Span | None:
    if passage.char_start is None or passage.char_end is None:
        return None
    return Span(passage.char_start, passage.char_end)


def _build_window(
    passage: Passage,
    plan: WindowPlan,
    chain: Sequence[DocumentNode],
    raw: str | None,
) -> PassageWindow | None:
    # None means the document has no canonical text; "" means an empty slice.
    # Neither is something to hand a reader.
    if not raw:
        return None

    requested = plan.span.start
    # The true end comes from what came back, not from what was asked for:
    # `get_span` clamps at the end of the text without saying so.
    start, end = requested, requested + len(raw)

    # `trim_span` indexes into the text it is given, so it takes offsets into
    # *this slice*, not into the document. Translate afterwards.
    lo, hi = trim_span(raw, 0, len(raw))
    start, end = requested + lo, requested + hi

    # Trimming can eat into the passage itself when the window opens on
    # whitespace inside it, so the floor is re-applied here rather than trusted
    # from `choose_window`.
    span = _passage_span(passage)
    if span is not None:
        start = min(start, max(span.start, requested))
        end = max(end, min(span.end, requested + len(raw)))

    text = raw[start - requested : end - requested]
    if not text:
        return None

    return PassageWindow(
        text=text,
        char_start=start,
        char_end=end,
        source=plan.source,
        node_id=plan.node.id if plan.node else None,
        breadcrumb=[n.title for n in chain if n.title],
        approx_tokens=approx_tokens(text),
    )
