"""work_create tool — start a work and its revision 1, both draft."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.mcp.errors import envelope, failed

logger = structlog.get_logger()

TOOL_NAME = "work_create"
TOOL_DESCRIPTION = (
    "Create an authored work and its revision 1 as draft. The slug must be "
    "unique. Blocks, citations, and links are added with work_block_upsert, "
    "work_cite, and work_link; the revision freezes with work_freeze."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "Unique kebab-case slug."},
        "title": {"type": "string"},
        "work_type": {
            "type": "string",
            "description": "translation, essay, dossier, script, outline, or pack-defined.",
        },
        "language": {"type": "string"},
        "abstract": {"type": "string"},
    },
    "required": ["slug", "title", "work_type"],
}


async def handler(
    container: Any,
    *,
    slug: str,
    title: str,
    work_type: str,
    language: str | None = None,
    abstract: str | None = None,
) -> dict[str, Any]:
    service = container.work_service
    try:
        created = await service.create(
            slug=slug, title=title, work_type=work_type,
            language=language, abstract=abstract,
        )
        return created.model_dump(mode="json")
    except ValueError as exc:
        return envelope("invalid_input", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_create_error", error=str(exc))
        return failed(TOOL_NAME, exc)
