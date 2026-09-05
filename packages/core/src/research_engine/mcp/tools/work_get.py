"""work_get tool — read a work's ordered block tree with citations inlined."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from research_engine.domain.errors import NotFoundError

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
        return {
            "error": {
                "code": "works_not_configured",
                "message": "The work service is not built.",
                "details": None,
            }
        }
    if slug is None and work_id is None:
        return {
            "error": {
                "code": "invalid_input",
                "message": "work_get needs slug or work_id",
                "details": None,
            }
        }
    work_uuid = None
    if work_id is not None:
        try:
            work_uuid = UUID(work_id)
        except (ValueError, TypeError):
            return {
                "error": {
                    "code": "invalid_input",
                    "message": f"work_id is not a UUID: {work_id}",
                    "details": None,
                }
            }
    try:
        return await service.get(slug=slug, work_id=work_uuid, revision=revision)
    except NotFoundError as exc:
        return {"error": {"code": "not_found", "message": str(exc), "details": None}}
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_get_error", error=str(exc))
        return {"error": {"code": "work_get_failed", "message": str(exc), "details": None}}
