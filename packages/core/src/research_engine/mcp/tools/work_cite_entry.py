"""work_cite_entry tool -- make a front-matter citation entry from a quote.

The file-phase companion to the Phase-1 `work_cite` (which writes
occurrence/item rows and owns that name per Appendix B): this one verifies,
resolves, and emits YAML to paste under a work file's `citations:`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from research_engine.mcp.errors import envelope, failed
from research_engine.services.works.cite import QuoteUnverifiedError

logger = structlog.get_logger()

TOOL_NAME = "work_cite_entry"
TOOL_DESCRIPTION = (
    "Make a citation entry for a work file. Give a document and the wording "
    "as typed: the quote is verified (exact or normalized to pass), its span "
    "is resolved — creating the span row on a miss — and the entry comes back "
    "with the VERIFIED offsets plus paste-ready YAML. Anything below "
    "exact/normalized is refused with nothing stored. Without a window the "
    "first occurrence wins; pass a search hit's span to pin a repeated quote. "
    "The id is echoed, never checked for collisions; work_verify judges "
    "narrowing and markers afterwards."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_id": {
            "type": "string",
            "format": "uuid",
            "description": "The cited document.",
        },
        "quoted_text": {
            "type": "string",
            "description": "The wording as typed. Whitespace is not significant.",
        },
        "intent": {
            "type": "string",
            "description": (
                "Why the citation is here: quotation, translation, support, "
                "contrast, background, definition, source, or see_also."
            ),
        },
        "id": {
            "type": "string",
            "description": "Citation handle, e.g. 'c1'. Omitted when the file owns numbering.",
        },
        "role": {
            "type": "string",
            "description": "Claim role, when this also backs a claim: asserts, supports, rebuts, context.",
        },
        "edition": {"type": "string"},
        "edition_key": {"type": "string"},
        "locator": {
            "type": "object",
            "description": "E.g. {page: 214} or {volume: 'II', page: 64}.",
        },
        "window": {
            "type": "object",
            "properties": {
                "char_start": {"type": "integer"},
                "char_end": {"type": "integer"},
            },
            "required": ["char_start", "char_end"],
            "description": "Where the quote is believed to sit, e.g. a search hit's span.",
        },
    },
    "required": ["document_id", "quoted_text", "intent"],
}


async def handler(
    container: Any,
    *,
    document_id: str,
    quoted_text: str,
    intent: str,
    id: str | None = None,
    role: str | None = None,
    edition: str | None = None,
    edition_key: str | None = None,
    locator: dict[str, Any] | None = None,
    window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    citer = container.work_citer
    try:
        doc_uuid = UUID(document_id)
    except (ValueError, TypeError):
        return envelope("invalid_input", f"document_id is not a UUID: {document_id}", None)
    window_tuple = _checked_window(window)
    if window is not None and window_tuple is None:
        return envelope("invalid_input", "window must be {char_start: int >= 0, char_end: int} "
                    "with char_end > char_start", None)
    try:
        result = await citer.cite(
            document_id=doc_uuid,
            quoted_text=quoted_text,
            intent=intent,
            citation_id=id,
            role=role,
            edition=edition,
            edition_key=edition_key,
            locator=locator,
            window=window_tuple,
        )
        return result.model_dump(mode="json")
    except ValueError as exc:
        return envelope("invalid_input", str(exc), None)
    except QuoteUnverifiedError as exc:
        return envelope("quote_unverified", exc.detail, {"tier": exc.tier.value, "divergence": exc.divergence})
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_cite_error", error=str(exc))
        return failed(TOOL_NAME, exc)


def _checked_window(window: dict[str, Any] | None) -> tuple[int, int] | None:
    """Validate the nested window object dispatch leaves unchecked.

    A copy of verify_quote's validator: dispatch checks only top-level
    `type`/`enum`, so a missing key or a bool for an offset is the handler's
    problem. Two copies are the wrong shape past two; a third window consumer
    should hoist this into one shared helper.
    """
    if window is None:
        return None
    if not isinstance(window, dict):
        return None
    start, end = window.get("char_start"), window.get("char_end")
    # bool is an int subclass; True is not an offset.
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or isinstance(start, bool)
        or isinstance(end, bool)
    ):
        return None
    if start < 0 or end <= start:
        return None
    return (start, end)
