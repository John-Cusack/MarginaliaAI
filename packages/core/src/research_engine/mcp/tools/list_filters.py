"""list_available_filters tool -- discover what filters are available."""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

TOOL_NAME = "list_available_filters"
TOOL_DESCRIPTION = (
    "List all available search filter dimensions including core filters "
    "and plugin-contributed filter extensions. Use this to discover what "
    "filtering is possible before calling find_passages."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}


async def handler(
    container: Any,
) -> dict[str, Any]:
    """Return all available filter dimensions."""
    try:
        registry = container.registry

        # Core filters (always available)
        core_filters = [
            {
                "id": "document_type",
                "description": "Restrict to specific document types.",
                "schema": {"type": "array", "items": {"type": "string"}},
            },
            {
                "id": "author_entity_id",
                "description": "Filter by author entity UUID.",
                "schema": {"type": "string", "format": "uuid"},
            },
            {
                "id": "recipient_entity_id",
                "description": "Filter by recipient entity UUID.",
                "schema": {"type": "string", "format": "uuid"},
            },
            {
                "id": "date_range",
                "description": "Filter by document creation date range (ISO 8601).",
                "schema": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                },
            },
            {
                "id": "mentions_entity_ids",
                "description": "Passages mentioning all of these entities.",
                "schema": {"type": "array", "items": {"type": "string", "format": "uuid"}},
            },
            {
                "id": "metadata",
                "description": "JSONB containment match on passage metadata.",
                "schema": {"type": "object"},
            },
        ]

        # Plugin filter extensions
        extensions = []
        for ext_id, ext_obj in registry.get_filter_extensions().items():
            extensions.append({
                "id": ext_id,
                "description": ext_obj.description,
                "schema": ext_obj.input_schema,
            })

        # Registered types (available via loaded plugins, not necessarily populated with data)
        document_types = list(registry.list_document_types().keys())
        entity_types = list(registry.list_entity_types().keys())
        event_types = list(registry.list_event_types().keys())

        return {
            "core_filters": core_filters,
            "extensions": extensions,
            "available_document_types": document_types,
            "available_entity_types": entity_types,
            "available_event_types": event_types,
        }
    except Exception as e:
        logger.error("list_filters_error", error=str(e))
        return {"error": {"code": "list_filters_failed", "message": str(e), "details": None}}
