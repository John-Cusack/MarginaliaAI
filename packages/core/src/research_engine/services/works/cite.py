"""Make citations — verify a quote, resolve its span, emit a paste-ready entry.

The only MCP path that writes: on a verified quote the span row is resolved
(created on a miss) and the entry carries the VERIFIED offsets, never
caller-supplied ones — the caller names a document and a wording, the corpus
names the address. Anything below exact/normalized is refused with nothing
stored: a span that does not exist is not evidence, and there is no mirror to
record the failure in (Step 4 was skipped) nor a verify_attempts table yet
(Appendix E.9), so the refusal itself is the whole record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.domain.works_files import ENTRY_ID_RE, CitationEntry, Intent, Role
from research_engine.services.verification.quote import Tier

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine


class QuoteUnverifiedError(Exception):
    """The quote refused to verify, so no span was resolved and nothing stored."""

    def __init__(self, tier: Tier, detail: str, divergence: dict[str, Any] | None) -> None:
        super().__init__(detail)
        self.tier = tier
        self.detail = detail
        self.divergence = divergence


class CitationResult(BaseModel):
    """A citation entry ready to paste under a work file's `citations:`."""

    entry: dict[str, Any]
    #: The same entry rendered as YAML, for pasting.
    entry_yaml: str
    tier: str
    verified_span: list[int]
    #: The resolved `evidence.source_spans` row, for future anchors.
    span_id: str


class WorkCiter:
    """Verify-then-resolve: the write boundary for new citations."""

    def __init__(self, verification: Any, spans: Any, engine: AsyncEngine) -> None:
        self._verification = verification
        self._spans = spans
        self._engine = engine

    async def cite(
        self,
        *,
        document_id: UUID,
        quoted_text: str,
        intent: str,
        citation_id: str | None = None,
        role: str | None = None,
        edition: str | None = None,
        edition_key: str | None = None,
        locator: dict[str, Any] | None = None,
        window: tuple[int, int] | None = None,
    ) -> CitationResult:
        """Verify *quoted_text* and resolve its address into a citation entry.

        Raises:
            ValueError: an entry field itself is malformed (unknown intent,
                bad id, non-object locator). The quote was never checked.
            QuoteUnverifiedError: the quote verified below exact/normalized.
                Nothing was stored.
        """
        try:
            parsed_intent = Intent(intent)
        except ValueError:
            raise ValueError(f"Unknown intent {intent!r}") from None
        parsed_role = None
        if role is not None:
            try:
                parsed_role = Role(role)
            except ValueError:
                raise ValueError(f"Unknown role {role!r}") from None
        if citation_id is not None and not ENTRY_ID_RE.match(citation_id):
            raise ValueError(f"id must match ^c[0-9]+$, got {citation_id!r}")
        if locator is not None and not isinstance(locator, dict):
            raise ValueError("locator must be an object")

        result = await self._verification.verify(
            quoted_text, document_id, window=window
        )
        if result.tier not in (Tier.EXACT, Tier.NORMALIZED) or result.location is None:
            raise QuoteUnverifiedError(
                tier=result.tier,
                detail=(
                    f"Quote verifies {result.tier.value}: only exact or "
                    "normalized can be cited. Nothing was stored."
                ),
                divergence=(
                    result.divergence.model_dump() if result.divergence else None
                ),
            )
        start, end = result.location.char_start, result.location.char_end
        async with transaction(self._engine) as tx:
            span = await self._spans.resolve(
                tx, document_id=document_id, char_start=start, char_end=end
            )

        entry = CitationEntry(
            id=citation_id or "c0",
            intent=parsed_intent,
            role=parsed_role,
            document_id=document_id,
            char_start=start,
            char_end=end,
            quoted_text=quoted_text,
            edition=edition,
            edition_key=edition_key,
            locator=locator or {},
        )
        dumped = entry.model_dump(mode="json", exclude_none=True)
        if citation_id is None:
            # The file owns numbering; c0 only satisfied the validator.
            del dumped["id"]
        else:
            dumped["id"] = citation_id
        if dumped.get("locator") == {}:
            del dumped["locator"]
        return CitationResult(
            entry=dumped,
            entry_yaml=yaml.safe_dump(dumped, sort_keys=False, allow_unicode=True),
            tier=result.tier.value,
            verified_span=[start, end],
            span_id=str(span.id),
        )
