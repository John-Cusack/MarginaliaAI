"""work_export tool — render one revision of a work to markdown text."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.domain.errors import NotFoundError
from research_engine.mcp.errors import envelope, failed

logger = structlog.get_logger()

TOOL_NAME = "work_export"
TOOL_DESCRIPTION = (
    "Render one revision of an authored work to §6.4 markdown text, returned "
    "inline — the server never reads or writes vault files, so pass the text "
    "onward yourself. Omit revision for the current draft; pass a number for "
    "frozen history (its base stamp predates current, so importing it back "
    "is refused as stale)."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "revision": {
            "type": "integer",
            "description": "Revision number; omit for the current revision.",
        },
    },
    "required": ["slug"],
}


async def handler(
    container: Any,
    *,
    slug: str,
    revision: int | None = None,
) -> dict[str, Any]:
    service = container.work_export
    try:
        rendered = await service.export_draft(slug=slug, revision=revision)
        return {"markdown": rendered}
    except NotFoundError as exc:
        return envelope("not_found", str(exc), None)
    except ValueError as exc:
        return envelope("invalid_input", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_export_error", error=str(exc))
        return failed(TOOL_NAME, exc)
