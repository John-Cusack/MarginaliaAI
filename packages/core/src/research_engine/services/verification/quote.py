"""Does the source actually say this?

The question a researcher asks last, and the one the corpus could not answer.
Search finds passages; nothing until now could take a quotation someone had
already written down and confirm it against the text, with a locator to cite.

Three things make this harder than a substring search, and each shapes the code:

**A quotation is rarely character-identical to its source.** Someone retyping
from a printed edition produces straight quotes where the OCR has curly ones,
a hyphen for an em dash, single spaces where the scan has a column break. So
matching happens at two tiers, and *the tiers are reported separately and never
collapsed*: `exact` means the source says precisely this, `normalized` means it
says this modulo typography. A researcher deciding whether to put something in
quotation marks needs to know which.

**A quotation straddles chunk boundaries.** It is a span of the document, not of
a passage, and a quote crossing a boundary matches no passage at all. So the
search runs against `document_texts` and the resulting span is mapped onto every
passage it touches.

**"Not found" and "cannot check" are different answers.** A document with no
canonical text can never be verified against, and reporting that as `not_found`
would teach someone to distrust a tool that was never given anything to read.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

import structlog
from pydantic import BaseModel, Field

from research_engine.services.text.anchoring import Span
from research_engine.services.text.normalize import (
    normalize,
    normalize_for_matching,
    normalize_with_map,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = structlog.get_logger()

#: Fraction of a quotation that must match before a miss is worth reporting as a
#: near miss rather than a plain absence. Below this the "closest match" is
#: usually a common phrase and pointing at it is misleading.
DEFAULT_NEAR_THRESHOLD = 0.5

#: How many documents a corpus-wide check will open. The trigram index narrows
#: to a handful; this only bounds a pathological query like a single word.
MAX_CANDIDATES = 10

#: Characters of context shown either side of where a near miss diverges.
DIVERGENCE_CONTEXT = 80


class Tier(StrEnum):
    """How closely the source matched, from the caller's point of view."""

    EXACT = "exact"
    NORMALIZED = "normalized"
    NEAR = "near"
    NOT_FOUND = "not_found"
    NO_CANONICAL_TEXT = "no_canonical_text"


class QuoteLocation(BaseModel):
    """Where a verified quotation sits in its source."""

    document_id: UUID
    document_title: str | None = None
    char_start: int
    char_end: int
    #: What the document actually says at this span, in its raw form. For a
    #: `normalized` match this is the interesting field: it is the typography the
    #: quotation smoothed over.
    source_text: str
    passage_ids: list[UUID] = Field(default_factory=list)
    #: Locators of the covering passages — page numbers where the ingest
    #: recorded them. Empty when the source carried none.
    locators: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def straddles_passages(self) -> bool:
        return len(self.passage_ids) > 1


class Divergence(BaseModel):
    """Where a near-miss quotation stops agreeing with the source."""

    matched_characters: int
    #: The tail of the quotation that matched, for orientation.
    matched_tail: str
    #: What the quotation says next.
    quote_continues: str
    #: What the source says next instead.
    source_continues: str


class QuoteVerification(BaseModel):
    tier: Tier
    quote: str
    location: QuoteLocation | None = None
    #: For `near`: how much of the quotation matched before diverging.
    matched_fraction: float | None = None
    divergence: Divergence | None = None
    documents_checked: int = 0
    detail: str = ""

    @property
    def verified(self) -> bool:
        """True only for a match a citation can rest on."""
        return self.tier in (Tier.EXACT, Tier.NORMALIZED)


class QuoteVerifier:
    def __init__(
        self,
        document_texts: Any,
        passages: Any,
        documents: Any,
        *,
        near_threshold: float = DEFAULT_NEAR_THRESHOLD,
        max_candidates: int = MAX_CANDIDATES,
    ) -> None:
        self._texts = document_texts
        self._passages = passages
        self._documents = documents
        self._near_threshold = near_threshold
        self._max_candidates = max_candidates

    async def verify(
        self, quote: str, document_id: UUID | None = None
    ) -> QuoteVerification:
        quote = quote.strip()
        if not quote:
            return QuoteVerification(
                tier=Tier.NOT_FOUND, quote=quote, detail="The quotation is empty."
            )

        # `normalize` for anything compared against the stored normalized_text
        # column, which was written by it. `normalize_for_matching` for anything
        # compared against `normalize_with_map` output. Mixing them silently
        # fails to match on combining marks.
        stored_form = normalize(quote)
        match_form = normalize_for_matching(quote)

        if document_id is not None:
            if await self._texts.lengths(document_id) is None:
                return QuoteVerification(
                    tier=Tier.NO_CANONICAL_TEXT,
                    quote=quote,
                    documents_checked=0,
                    detail=(
                        f"Document {document_id} has no canonical text stored, so "
                        f"there is nothing to check the quotation against. This is "
                        f"not the same as the quotation being absent. Re-ingest "
                        f"the document to make it verifiable."
                    ),
                )
            candidates: Sequence[UUID] = [document_id]
        else:
            candidates = await self._texts.find_documents_containing(
                stored_form, limit=self._max_candidates
            )

        normalized_hit: QuoteVerification | None = None
        for candidate in candidates:
            found = await self._locate_exact(candidate, quote)
            if found is not None:
                return await self._resolve(
                    Tier.EXACT, quote, candidate, found, len(candidates),
                    detail="The source contains this quotation character for character.",
                )
            found = await self._locate_normalized(candidate, stored_form, match_form)
            if found is not None and normalized_hit is None:
                normalized_hit = await self._resolve(
                    Tier.NORMALIZED, quote, candidate, found, len(candidates),
                    detail=(
                        "The source contains this quotation apart from typography "
                        "— whitespace, quote marks, dashes or hyphenation. Compare "
                        "`source_text` before quoting it verbatim."
                    ),
                )
        if normalized_hit is not None:
            return normalized_hit

        return await self._near_miss(quote, stored_form, match_form, document_id)

    # --- locating -----------------------------------------------------------

    async def _locate_exact(self, document_id: UUID, quote: str) -> Span | None:
        at = await self._texts.find_raw(document_id, quote)
        return Span(at, at + len(quote)) if at is not None else None

    async def _locate_normalized(
        self, document_id: UUID, stored_form: str, match_form: str
    ) -> Span | None:
        """Find a typographically-folded quotation, in *raw* offsets.

        Postgres locates it in the normalized column; that offset is not a raw
        offset, and the largest document here is 23.2M characters, so folding the
        whole thing in Python to find out would take seconds every call.

        Instead the raw:normalized length ratio estimates where the match sits
        and only that window is folded. Whitespace density is near-uniform
        within a document — measured on this corpus, a normalized offset of 1337
        corresponded to a raw offset of 1345 — so the first window almost always
        contains it. The widening steps and the whole-document fallback exist
        because "almost always" is not a correctness argument.
        """
        norm_offset = await self._texts.find_normalized(document_id, stored_form)
        if norm_offset is None:
            return None
        lengths = await self._texts.lengths(document_id)
        if lengths is None:
            return None
        raw_len, norm_len = lengths
        approximate = int(norm_offset * raw_len / max(norm_len, 1))
        span = len(match_form) * 2

        for slack in (4_096, 65_536, 1_048_576):
            lo = max(0, approximate - slack)
            hi = min(raw_len, approximate + span + slack)
            window = await self._texts.get_span(document_id, lo, hi)
            if window is None:
                return None
            found = _find_folded(window, match_form)
            if found is not None:
                return Span(lo + found.start, lo + found.end)
            if lo == 0 and hi == raw_len:
                return None  # the window was already the whole document

        raw = await self._texts.get_text(document_id)
        if raw is None:
            return None
        logger.info(
            "quote_window_missed_falling_back",
            document_id=str(document_id),
            raw_length=raw_len,
            detail="Windowed lookup missed; folding the whole document.",
        )
        return _find_folded(raw, match_form)

    async def _resolve(
        self,
        tier: Tier,
        quote: str,
        document_id: UUID,
        span: Span,
        checked: int,
        *,
        detail: str,
    ) -> QuoteVerification:
        """Attach the source text, covering passages and locators to a hit."""
        source_text = await self._texts.get_span(document_id, span.start, span.end)
        covering = await self._passages.covering_span(document_id, span.start, span.end)
        document = await self._documents.get(document_id)
        return QuoteVerification(
            tier=tier,
            quote=quote,
            documents_checked=checked,
            detail=detail,
            location=QuoteLocation(
                document_id=document_id,
                document_title=getattr(document, "title", None),
                char_start=span.start,
                char_end=span.end,
                source_text=source_text or "",
                passage_ids=[p.id for p in covering],
                locators=[p.locator for p in covering if p.locator],
            ),
        )

    # --- near misses --------------------------------------------------------

    async def _near_miss(
        self,
        quote: str,
        stored_form: str,
        match_form: str,
        document_id: UUID | None,
    ) -> QuoteVerification:
        """How much of the quotation the corpus does contain, and where it stops.

        Answers the question a researcher actually has when a quote fails: not
        "is it there" but "which part did I get wrong". The longest matching
        prefix is found by binary search — each probe is one indexed LIKE, so
        this costs about ten queries rather than a scan.
        """
        prefix_len, holder = await self._longest_prefix(stored_form, document_id)
        fraction = prefix_len / len(stored_form) if stored_form else 0.0

        if holder is None or fraction < self._near_threshold:
            return QuoteVerification(
                tier=Tier.NOT_FOUND,
                quote=quote,
                matched_fraction=round(fraction, 3) if stored_form else None,
                documents_checked=0,
                detail=(
                    "No document contains this quotation, and no substantial "
                    "part of it either. Check the wording, or the document may "
                    "not be in the corpus."
                ),
            )

        matched_prefix = stored_form[:prefix_len]
        span = await self._locate_normalized(
            holder, matched_prefix, normalize_for_matching(matched_prefix)
        )
        source_continues = ""
        if span is not None:
            following = await self._texts.get_span(
                holder, span.end, span.end + DIVERGENCE_CONTEXT
            )
            source_continues = normalize(following or "")

        result = QuoteVerification(
            tier=Tier.NEAR,
            quote=quote,
            matched_fraction=round(fraction, 3),
            documents_checked=1,
            divergence=Divergence(
                matched_characters=prefix_len,
                matched_tail=matched_prefix[-DIVERGENCE_CONTEXT:],
                quote_continues=stored_form[prefix_len : prefix_len + DIVERGENCE_CONTEXT],
                source_continues=source_continues,
            ),
            detail=(
                f"The source matches the first {prefix_len} characters of this "
                f"quotation and then diverges. Compare `quote_continues` against "
                f"`source_continues`."
            ),
        )
        if span is not None:
            resolved = await self._resolve(
                Tier.NEAR, quote, holder, span, 1, detail=result.detail
            )
            result.location = resolved.location
        return result

    async def _longest_prefix(
        self, stored_form: str, document_id: UUID | None
    ) -> tuple[int, UUID | None]:
        """Binary search the longest prefix of the quotation that exists."""

        async def holder_of(length: int) -> UUID | None:
            prefix = stored_form[:length]
            if not prefix.strip():
                return None
            if document_id is not None:
                at = await self._texts.find_normalized(document_id, prefix)
                return document_id if at is not None else None
            found = await self._texts.find_documents_containing(prefix, limit=1)
            return found[0] if found else None

        best_len, best_holder = 0, None
        lo, hi = 1, len(stored_form)
        while lo <= hi:
            mid = (lo + hi) // 2
            holder = await holder_of(mid)
            if holder is not None:
                best_len, best_holder = mid, holder
                lo = mid + 1
            else:
                hi = mid - 1
        return best_len, best_holder


def _find_folded(raw: str, match_form: str) -> Span | None:
    """Locate a folded needle in *raw*, returning raw offsets."""
    folded, index_map = normalize_with_map(raw)
    at = folded.find(match_form)
    if at < 0:
        return None
    return Span(index_map[at], index_map[at + len(match_form) - 1] + 1)
