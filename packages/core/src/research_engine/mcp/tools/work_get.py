"""work_get tool — read a work's ordered block tree with citations inlined."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from research_engine.domain.errors import NotFoundError
from research_engine.mcp.errors import envelope, failed

logger = structlog.get_logger()

TOOL_NAME = "work_get"
TOOL_DESCRIPTION = (
    "Read a work: its revision plus the ordered block tree with citation "
    "occurrences, items, and links inlined per block. Defaults to the "
    "current revision; pass revision for an older numbered one."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "work_id": {"type": "string", "format": "uuid"},
        "revision": {"type": "integer", "description": "Revision number; omit for current."},
    },
}


async def handler(
    container: Any,
    *,
    slug: str | None = None,
    work_id: str | None = None,
    revision: int | None = None,
) -> dict[str, Any]:
    service = getattr(container, "work_service", None)
    if service is None:  # pragma: no cover - composition always builds it
        return envelope("works_not_configured", "The work service is not built.", None)
    if slug is None and work_id is None:
        return envelope("invalid_input", "work_get needs slug or work_id", None)
    work_uuid = None
    if work_id is not None:
        try:
            work_uuid = UUID(work_id)
        except (ValueError, TypeError):
            return envelope("invalid_input", f"work_id is not a UUID: {work_id}", None)
    try:
        return await service.get(slug=slug, work_id=work_uuid, revision=revision)
    except NotFoundError as exc:
        return envelope("not_found", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_get_error", error=str(exc))
        return failed(TOOL_NAME, exc)
