"""work_cite tool — ground a block's marker in the corpus.

The Phase-1 row writer (Appendix B): slug and block key in, occurrence and
item rows out. The quote is verified and the span resolved in the same
transaction as the rows; any refusal writes nothing and names its rule.
The returned marker is placed in the block text by the caller, and
`work_validate` judges the bijection afterwards.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from research_engine.domain.errors import FrozenRevisionError, NotFoundError
from research_engine.mcp.errors import envelope, failed
from research_engine.services.works.attach import AttachRefused

logger = structlog.get_logger()

TOOL_NAME = "work_cite"
TOOL_DESCRIPTION = (
    "Cite the corpus from a draft block. Give the work, the block, why the "
    "citation is here (intent), and a quote to verify. The edition is "
    "inherited from the cited document; pass edition_key or edition_id only "
    "to cite a different edition than the span's (the mismatch check tests "
    "that claim), or alone for a bibliography-only citation with no quote. "
    "A verified quote resolves its span — creating the span row on a miss — "
    "and the occurrence plus item are written atomically. Returns the "
    "{{cite:<key>}} marker to place in the block text. Refusals name their "
    "rule id and write nothing: AUTH_QUOTE_UNVERIFIED (below near), "
    "AUTH_SPAN_NOT_NARROWED (quotation/translation on a region), "
    "AUTH_CITATION_EDITION_MISSING (no identity), AUTH_SOURCE_UNCHECKABLE "
    "(no canonical text). Without a window the first occurrence wins; pass "
    "a search hit's span to pin a repeated quote."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "block_key": {"type": "string", "format": "uuid"},
        "intent": {
            "type": "string",
            "enum": [
                "source", "support", "contrast", "background", "definition",
                "translation", "quotation", "see_also",
            ],
        },
        "quote": {"type": "string", "description": "The wording as typed."},
        "document_id": {"type": "string", "format": "uuid"},
        "window": {
            "type": "object",
            "properties": {
                "char_start": {"type": "integer"},
                "char_end": {"type": "integer"},
            },
            "required": ["char_start", "char_end"],
            "description": "Where the quote is believed to sit, e.g. a search hit's span.",
        },
        "edition_key": {"type": "string"},
        "edition_id": {"type": "string", "format": "uuid"},
        "locator": {"type": "object", "description": "E.g. {page: 214}."},
        "prefix": {"type": "string"},
        "suffix": {"type": "string"},
        "placement": {"type": "string", "enum": ["inline", "block_end"]},
        "citation_key": {"type": "string", "format": "uuid"},
    },
    "required": ["slug", "block_key", "intent"],
}


async def handler(
    container: Any,
    *,
    slug: str,
    block_key: str,
    intent: str,
    quote: str | None = None,
    document_id: str | None = None,
    window: dict[str, Any] | None = None,
    edition_key: str | None = None,
    edition_id: str | None = None,
    locator: dict[str, Any] | None = None,
    prefix: str | None = None,
    suffix: str | None = None,
    placement: str = "inline",
    citation_key: str | None = None,
) -> dict[str, Any]:
    service = container.citation_service
    try:
        block_uuid = UUID(block_key)
        doc_uuid = UUID(document_id) if document_id is not None else None
        edition_uuid = UUID(edition_id) if edition_id is not None else None
        cite_uuid = UUID(citation_key) if citation_key is not None else None
    except (ValueError, TypeError):
        return envelope("invalid_input", "block_key, document_id, edition_id, and citation_key must be UUIDs", None)
    window_tuple = _checked_window(window)
    if window is not None and window_tuple is None:
        return envelope("invalid_input", "window must be {char_start: int >= 0, char_end: int} "
                    "with char_end > char_start", None)
    try:
        attached = await service.attach(
            slug=slug,
            block_key=block_uuid,
            intent=intent,
            quote=quote,
            document_id=doc_uuid,
            window=window_tuple,
            edition_key=edition_key,
            edition_id=edition_uuid,
            locator=locator,
            prefix=prefix,
            suffix=suffix,
            placement=placement,
            citation_key=cite_uuid,
        )
        return attached.model_dump(mode="json")
    except AttachRefused as exc:
        return envelope("validation_error", exc.message, {"rule_id": exc.rule_id, **(exc.detail or {})})
    except NotFoundError as exc:
        return envelope("not_found", str(exc), None)
    except FrozenRevisionError as exc:
        return envelope("conflict", str(exc), None)
    except ValueError as exc:
        return envelope("invalid_input", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_cite_error", error=str(exc))
        return failed(TOOL_NAME, exc)


def _checked_window(window: dict[str, Any] | None) -> tuple[int, int] | None:
    """Validate the nested window object dispatch leaves unchecked."""
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
