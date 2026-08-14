"""get_entity tool -- fetch full entity record."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger()

TOOL_NAME = "get_entity"
TOOL_DESCRIPTION = (
    "Fetch a full entity record by ID, including all aliases and attributes."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the entity to retrieve.",
        },
    },
    "required": ["entity_id"],
}


async def handler(
    container: Any,
    *,
    entity_id: str,
) -> dict[str, Any]:
    """Fetch a full entity record with aliases."""
    try:
        entity_service = container.entity_service

        eid = UUID(entity_id)
        entity, aliases = await entity_service.get_with_aliases(eid)

        if not entity:
            return {"error": {"code": "not_found", "message": f"Entity not found: {entity_id}", "details": None}}

        return {
            "id": str(entity.id),
            "entity_type": entity.entity_type,
            "canonical_name": entity.canonical_name,
            "disambiguator": entity.disambiguator,
            "aliases": [a.alias for a in aliases],
            "attributes": entity.attributes,
            "created_at": entity.created_at.isoformat(),
            "updated_at": entity.updated_at.isoformat(),
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("get_entity_error", error=str(e))
        return {"error": {"code": "get_entity_failed", "message": str(e), "details": None}}
