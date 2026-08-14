"""upsert_entity tool -- create or update an entity record."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.domain.entities import EntityDraft

logger = structlog.get_logger()

TOOL_NAME = "upsert_entity"
TOOL_DESCRIPTION = (
    "Create or update an entity record. If an entity with a very similar "
    "name already exists, it will be updated; otherwise a new entity is "
    "created. Requires write capability grant."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_type": {
            "type": "string",
            "description": "Type of entity (e.g. 'person', 'place', 'organization').",
        },
        "canonical_name": {
            "type": "string",
            "description": "Canonical display name for the entity.",
        },
        "disambiguator": {
            "type": "string",
            "description": "Optional disambiguator (e.g. 'general', 'politician').",
        },
        "attributes": {
            "type": "object",
            "description": "Arbitrary key-value attributes for the entity.",
        },
        "aliases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Alternative names or spellings.",
        },
    },
    "required": ["entity_type", "canonical_name"],
}


async def handler(
    container: Any,
    *,
    entity_type: str,
    canonical_name: str,
    disambiguator: str | None = None,
    attributes: dict[str, Any] | None = None,
    aliases: list[str] | None = None,
) -> dict[str, Any]:
    """Create or update an entity."""
    try:
        entity_service = container.entity_service
        tx_factory = container.transaction_factory

        draft = EntityDraft(
            entity_type=entity_type,
            canonical_name=canonical_name,
            disambiguator=disambiguator,
            attributes=attributes or {},
        )

        async with tx_factory() as tx:
            entity = await entity_service.upsert(tx, draft, aliases=aliases)

        return {
            "id": str(entity.id),
            "entity_type": entity.entity_type,
            "canonical_name": entity.canonical_name,
            "disambiguator": entity.disambiguator,
            "attributes": entity.attributes,
            "created_at": entity.created_at.isoformat(),
            "updated_at": entity.updated_at.isoformat(),
        }
    except Exception as e:
        logger.error("upsert_entity_error", error=str(e))
        return {"error": {"code": "upsert_entity_failed", "message": str(e), "details": None}}
