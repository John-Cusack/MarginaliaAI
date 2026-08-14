"""find_mentions tool -- all passages mentioning an entity."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger()

TOOL_NAME = "find_mentions"
TOOL_DESCRIPTION = (
    "Find all passages that mention a given entity, with optional filters "
    "on date range and document type."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the entity to find mentions for.",
        },
        "filters": {
            "type": "object",
            "description": "Optional filters for narrowing mentions.",
            "properties": {
                "date_range": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                },
                "document_type": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "k": {
            "type": "integer",
            "default": 100,
            "description": "Maximum number of mentions to return.",
        },
    },
    "required": ["entity_id"],
}


async def handler(
    container: Any,
    *,
    entity_id: str,
    filters: dict[str, Any] | None = None,
    k: int = 100,
) -> dict[str, Any]:
    """Find mentions of an entity across passages."""
    try:
        entity_service = container.entity_service

        eid = UUID(entity_id)
        mentions = await entity_service.find_mentions(eid, filters=filters, k=k)

        return {
            "mentions": [
                {
                    "mention_id": str(m.id),
                    "passage_id": str(m.passage_id),
                    "entity_id": str(m.entity_id),
                    "span_start": m.span_start,
                    "span_end": m.span_end,
                    "surface_form": m.surface_form,
                    "confidence": m.confidence,
                    "source": m.source.value if hasattr(m.source, "value") else str(m.source),
                }
                for m in mentions
            ],
            "total": len(mentions),
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("find_mentions_error", error=str(e))
        return {"error": {"code": "find_mentions_failed", "message": str(e), "details": None}}
