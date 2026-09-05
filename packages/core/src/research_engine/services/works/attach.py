"""Attach a citation to a block — the Phase-1 `work_cite` write boundary.

One transaction: resolve the block in the work's current draft revision,
resolve the bibliographic identity, verify the quote when one is given,
apply the narrowing rule for the intent, resolve the span, and insert the
occurrence plus its item. Any refusal writes nothing — a span that does not
exist is not evidence — and the refusal carries its rule id so the tool can
return it verbatim.

`near` quotes are stored, not refused: the tier rides on the item row and
`work_validate` clears it only against a waiver row at freeze. `not_found`
and `no_canonical_text` store nothing, because there is no address to store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

import structlog
from pydantic import BaseModel, Field
from uuid_utils import uuid7

from research_engine.domain.citations import CitationItemDraft, OccurrenceDraft
from research_engine.domain.errors import FrozenRevisionError, NotFoundError
from research_engine.domain.works import Placement
from research_engine.domain.works_files import Intent
from research_engine.services.verification.quote import Tier
from research_engine.services.works.markers import format_marker
from research_engine.services.works.verify import MAX_QUOTE_CHARS

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger()

#: Intents that must cite a narrowed span, never a whole passage.
_NARROW_INTENTS = frozenset({Intent.QUOTATION, Intent.TRANSLATION})
#: Intents warned when they cite a whole passage.
_REGION_WARN_INTENTS = frozenset({Intent.SUPPORT, Intent.SOURCE, Intent.DEFINITION})


class AttachRefused(Exception):
    """The citation was refused with nothing written; the tool reports the rule."""

    def __init__(
        self, rule_id: str, message: str, detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.message = message
        self.detail = detail


class AttachedItem(BaseModel):
    source_span_id: UUID | None = None
    char_start: int | None = None
    char_end: int | None = None
    verify_status: str | None = None
    edition_id: UUID | None = None
    zotero_key: str | None = None


class CitationAttached(BaseModel):
    occurrence_id: UUID
    citation_key: UUID
    marker: str
    item: AttachedItem
    warnings: list[str] = Field(default_factory=list)


class CitationService:
    """Verify-then-write for citation rows, per guide §6.3."""

    def __init__(
        self,
        *,
        verification: Any,
        spans: Any,
        editions: Any,
        citations: Any,
        works: Any,
        revisions: Any,
        blocks: Any,
        passages: Any,
        transaction_factory: Callable[[], Any],
    ) -> None:
        self._verification = verification
        self._spans = spans
        self._editions = editions
        self._citations = citations
        self._works = works
        self._revisions = revisions
        self._blocks = blocks
        self._passages = passages
        self._transaction = transaction_factory

    async def attach(
        self,
        *,
        slug: str,
        block_key: UUID,
        intent: str,
        quote: str | None = None,
        document_id: UUID | None = None,
        window: tuple[int, int] | None = None,
        zotero_key: str | None = None,
        edition_id: UUID | None = None,
        locator: dict[str, Any] | None = None,
        prefix: str | None = None,
        suffix: str | None = None,
        placement: str = "inline",
        citation_key: UUID | None = None,
    ) -> CitationAttached:
        """Ground a block's marker in the corpus, or refuse with a rule id."""
        try:
            parsed_intent = Intent(intent)
        except ValueError:
            raise ValueError(f"Unknown intent {intent!r}") from None
        try:
            parsed_placement = Placement(placement)
        except ValueError:
            raise ValueError(f"Unknown placement {placement!r}") from None

        work = await self._works.get_by_slug(slug)
        if work is None:
            raise NotFoundError("work", slug)
        if work.current_revision_id is None:
            raise NotFoundError("work_revision", f"current of {slug}")
        revision = await self._revisions.get(work.current_revision_id)
        if revision is None:  # pragma: no cover - FK keeps the pointer whole
            raise NotFoundError("work_revision", work.current_revision_id)
        if revision.state.value != "draft":
            raise FrozenRevisionError(
                f"Revision {revision.id} is {revision.state.value}, not draft: "
                "copy it forward to edit."
            )
        block = await self._blocks.by_key(revision.id, block_key)
        if block is None:
            raise NotFoundError("work_block", block_key)

        resolved_edition_id = edition_id
        if resolved_edition_id is None and zotero_key is not None:
            edition = await self._editions.get_by_key(zotero_key)
            if edition is not None:
                resolved_edition_id = edition.id
        if resolved_edition_id is None and zotero_key is None:
            raise AttachRefused(
                "AUTH_CITATION_EDITION_MISSING",
                "A citation names its edition: pass zotero_key or edition_id. "
                "Nothing was written.",
            )

        tier: str | None = None
        span_document_id: UUID | None = None
        char_start: int | None = None
        char_end: int | None = None
        warnings: list[str] = []
        if quote is not None:
            verified = await self._verification.verify(
                quote, document_id, window=window
            )
            if verified.tier is Tier.NO_CANONICAL_TEXT:
                raise AttachRefused(
                    "AUTH_SOURCE_UNCHECKABLE",
                    f"Document {document_id} has no canonical text, so the "
                    "quote cannot be checked. Nothing was written.",
                    detail={"tier": verified.tier.value},
                )
            if verified.tier is Tier.NOT_FOUND:
                raise AttachRefused(
                    "AUTH_QUOTE_UNVERIFIED",
                    "Quote verifies not_found: only exact, normalized, or "
                    "near can be cited. Nothing was written.",
                    detail={
                        "tier": verified.tier.value,
                        "divergence": verified.divergence.model_dump()
                        if verified.divergence
                        else None,
                    },
                )
            if verified.location is None:
                # A near miss whose prefix will not locate names no address:
                # nothing to resolve, so the refusal is the whole record.
                raise AttachRefused(
                    "AUTH_QUOTE_UNVERIFIED",
                    "Quote verifies near but the matching part will not "
                    "locate: only a located span can be cited. Nothing was "
                    "written.",
                    detail={"tier": verified.tier.value},
                )
            tier = verified.tier.value
            span_document_id = verified.location.document_id
            char_start = verified.location.char_start
            char_end = verified.location.char_end
            if parsed_intent in _NARROW_INTENTS and (
                await self._is_region(
                    verified.location.document_id, char_start, char_end
                )
                or char_end - char_start > MAX_QUOTE_CHARS
            ):
                raise AttachRefused(
                    "AUTH_SPAN_NOT_NARROWED",
                    "A quotation or translation must cite a narrowed span, "
                    "not a whole passage. Nothing was written.",
                )
            if parsed_intent in _REGION_WARN_INTENTS and await self._is_region(
                verified.location.document_id, char_start, char_end
            ):
                warnings.append("AUTH_SPAN_REGION")

        # uuid_utils ids never cross into pydantic (see WorkService.upsert_block).
        key = citation_key or UUID(str(uuid7()))
        span_id: UUID | None = None
        async with self._transaction() as tx:
            if char_start is not None:
                assert char_end is not None and span_document_id is not None
                span = await self._spans.resolve(
                    tx,
                    document_id=span_document_id,
                    char_start=char_start,
                    char_end=char_end,
                )
                span_id = span.id
            occurrence = await self._citations.insert_occurrence(
                tx,
                OccurrenceDraft(
                    block_id=block.id,
                    citation_key=key,
                    placement=parsed_placement,
                    intent=parsed_intent,
                ),
            )
            await self._citations.insert_item(
                tx,
                CitationItemDraft(
                    occurrence_id=occurrence.id,
                    position=0,
                    edition_id=resolved_edition_id,
                    zotero_key=zotero_key,
                    source_span_id=span_id,
                    quoted_text=quote,
                    verify_status=tier,
                    locator=locator or {},
                    prefix=prefix,
                    suffix=suffix,
                ),
            )
        logger.info(
            "citation_attached", slug=slug, block_key=str(block_key),
            citation_key=str(key), tier=tier,
        )
        return CitationAttached(
            occurrence_id=occurrence.id,
            citation_key=key,
            marker=format_marker(key),
            item=AttachedItem(
                source_span_id=span_id,
                char_start=char_start,
                char_end=char_end,
                verify_status=tier,
                edition_id=resolved_edition_id,
                zotero_key=zotero_key,
            ),
            warnings=warnings,
        )

    async def _is_region(
        self, document_id: UUID, char_start: int, char_end: int
    ) -> bool:
        """Whether this span coincides with one passage row's bounds."""
        covering = await self._passages.covering_span(
            document_id, char_start, char_end
        )
        return any(
            passage.char_start == char_start and passage.char_end == char_end
            for passage in covering
        )
