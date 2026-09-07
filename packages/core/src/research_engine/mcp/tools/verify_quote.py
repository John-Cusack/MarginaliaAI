"""verify_quote tool -- check a quotation against what the source actually says."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger()

TOOL_NAME = "verify_quote"
TOOL_DESCRIPTION = (
    "Check a quotation against the corpus and report where it comes from. "
    "Returns one of five answers, and the distinction between them matters: "
    "'exact' — the source says precisely this; 'normalized' — it says this "
    "apart from typography (curly quotes, dashes, line-break hyphenation, "
    "whitespace), so compare source_text before quoting verbatim; 'near' — part "
    "of it matches and the response shows where it diverges; 'not_found' — no "
    "document contains it; 'no_canonical_text' — the named document has no "
    "stored text, so nothing could be checked, which is NOT the same as the "
    "quotation being absent. Never report a 'normalized' match as exact. Use it "
    "before citing anything, and to locate a quotation you already have in "
    "notes. Pass document_id when you know the source — it is faster and "
    "enables near-miss diagnosis."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "The quotation to check. Whitespace is not significant.",
        },
        "document_id": {
            "type": "string",
            "format": "uuid",
            "description": (
                "Restrict the check to one document. Omit to search the corpus."
            ),
        },
        "window": {
            "type": "object",
            "properties": {
                "char_start": {"type": "integer"},
                "char_end": {"type": "integer"},
            },
            "required": ["char_start", "char_end"],
            "description": (
                "Where the quotation is believed to sit — the span from a "
                "search hit. Checked first; on a miss the whole document is "
                "searched as usual. Needs document_id to mean anything."
            ),
        },
    },
    "required": ["text"],
}


async def handler(
    container: Any,
    *,
    text: str,
    document_id: str | None = None,
    window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    window_tuple = _checked_window(window)
    if window is not None and window_tuple is None:
        return {
            "error": {
                "code": "invalid_input",
                "message": (
                    "window must be {char_start: int >= 0, char_end: int} "
                    "with char_end > char_start"
                ),
                "details": None,
            }
        }
    result = await container.verification.verify(
        text, UUID(document_id) if document_id else None, window=window_tuple
    )
    payload: dict[str, Any] = {
        "tier": result.tier.value,
        "verified": result.verified,
        "detail": result.detail,
        "documents_checked": result.documents_checked,
    }
    if result.location is not None:
        location = result.location
        payload["location"] = {
            "document_id": str(location.document_id),
            "document_title": location.document_title,
            "char_start": location.char_start,
            "char_end": location.char_end,
            "source_text": location.source_text,
            "passage_ids": [str(p) for p in location.passage_ids],
            "locators": location.locators,
            # The chunk's range answers "where was this retrieved"; this
            # answers "what do I cite". Both, because a caller wanting a
            # page number still needs the locator.
            "node": location.node.model_dump(mode="json") if location.node else None,
            # Surfaced because it explains an otherwise puzzling result: a quote
            # spanning two chunks matches no single passage, which is why this
            # searches document text rather than passage text.
            "straddles_passages": location.straddles_passages,
        }
    if result.matched_fraction is not None:
        payload["matched_fraction"] = result.matched_fraction
    if result.divergence is not None:
        payload["divergence"] = result.divergence.model_dump()
    return payload


def _checked_window(window: dict[str, Any] | None) -> tuple[int, int] | None:
    """Validate the nested window object dispatch leaves unchecked.

    Dispatch validates only top-level `type`/`enum`; a nested object arrives
    as-is, so a missing key or a bool for an offset is the handler's problem.
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
