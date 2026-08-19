"""provenance_of tool -- trace provenance chain for derived data."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger()

TOOL_NAME = "provenance_of"
TOOL_DESCRIPTION = (
    "Trace the provenance chain for any derived data item (extraction record, "
    "mention, or event) back to its source passage and document."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["extraction_record", "mention", "event"],
            "description": "The type of derived data to trace.",
        },
        "id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the derived data item.",
        },
    },
    "required": ["kind", "id"],
}


async def handler(
    container: Any,
    *,
    kind: str,
    id: str,
) -> dict[str, Any]:
    """Trace provenance of a derived data item."""
    try:
        passage_repo = container.passage_repo
        document_repo = container.document_repo

        item_id = UUID(id)
        passage_id: UUID | None = None
        extraction_info: dict[str, Any] = {}

        if kind == "extraction_record":
            extraction_repo = container.extraction_repo
            record = await extraction_repo.get_record(item_id)
            if record:
                passage_id = record.passage_id
                extraction_info = {
                    "schema_id": str(record.schema_id),
                    "evidence_start": record.evidence_start,
                    "evidence_end": record.evidence_end,
                }

        elif kind == "mention":
            # Look up by iterating -- in a real impl this would be a direct get
            # For now we surface what we can
            passage_id = None  # Would need a mention get-by-id method

        elif kind == "event":
            event_service = container.event_service
            event = await event_service.get(item_id)
            if event:
                passage_id = event.source_passage_id

        else:
            return {"error": {"code": "invalid_input", "message": f"Unknown kind: {kind}", "details": None}}

        if not passage_id:
            return {"error": {"code": "not_found", "message": f"Could not trace provenance for {kind}:{id}", "details": None}}

        # Load passage
        passage = await passage_repo.get(passage_id)
        if not passage:
            return {"error": {"code": "not_found", "message": f"Source passage not found: {passage_id}", "details": None}}

        # Load document
        document = await document_repo.get(passage.document_id)

        result: dict[str, Any] = {
            "passage": {
                "id": str(passage.id),
                "text": passage.text,
                "locator": passage.locator,
            },
            "document": {
                "id": str(document.id) if document else None,
                "title": document.title if document else None,
                "source": document.source if document else None,
            },
        }

        # Add evidence span if available
        if extraction_info.get("evidence_start") is not None:
            start = extraction_info["evidence_start"]
            end = extraction_info.get("evidence_end", start)
            result["evidence_span"] = {
                "start": start,
                "end": end,
                "text": passage.text[start:end] if start < len(passage.text) else "",
            }

        if extraction_info:
            result["extraction"] = extraction_info

        return result
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("provenance_of_error", error=str(e))
        return {"error": {"code": "provenance_of_failed", "message": str(e), "details": None}}
