"""resolve_entity tool -- entity lookup by name/type."""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

TOOL_NAME = "resolve_entity"
TOOL_DESCRIPTION = (
    "Look up or search entities by name, alias, or attributes. "
    "Uses tiered resolution: exact match, then alias match, then fuzzy."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Name or alias to search for.",
        },
        "entity_type": {
            "type": "string",
            "description": "Optional entity type filter (e.g. 'person', 'place', 'organization').",
        },
        "disambiguator": {
            "type": "string",
            "description": "Optional disambiguator hint (e.g. 'general', 'politician').",
        },
        "k": {
            "type": "integer",
            "default": 5,
            "description": "Maximum number of candidates to return.",
        },
    },
    "required": ["query"],
}


async def handler(
    container: Any,
    *,
    query: str,
    entity_type: str | None = None,
    disambiguator: str | None = None,
    k: int = 5,
) -> dict[str, Any]:
    """Resolve an entity by name."""
    try:
        entity_service = container.entity_service

        candidates = await entity_service.resolve(
            name=query,
            entity_type=entity_type,
            k=k,
        )

        # If disambiguator provided, prefer matches
        if disambiguator and candidates:
            exact = [c for c in candidates if c.disambiguator == disambiguator]
            rest = [c for c in candidates if c.disambiguator != disambiguator]
            candidates = exact + rest

        return {
            "candidates": [
                {
                    "entity_id": str(c.entity_id),
                    "canonical_name": c.canonical_name,
                    "entity_type": c.entity_type,
                    "disambiguator": c.disambiguator,
                    "aliases": c.aliases,
                    "attributes": c.attributes,
                    "match_score": c.match_score,
                }
                for c in candidates[:k]
            ],
        }
    except Exception as e:
        logger.error("resolve_entity_error", error=str(e))
        return {"error": {"code": "resolve_entity_failed", "message": str(e), "details": None}}
