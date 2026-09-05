"""work_link tool — type one edge from a block to a span or an entity."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from research_engine.domain.errors import FrozenRevisionError, NotFoundError

logger = structlog.get_logger()

TOOL_NAME = "work_link"
TOOL_DESCRIPTION = (
    "Link a draft block to the corpus: a source span (by id, or by document "
    "coordinates resolved on write) or an entity (renders, discusses). "
    "Exactly one target per call."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "block_key": {"type": "string", "format": "uuid"},
        "relation": {
            "type": "string",
            "description": "quotes, translates, discusses, renders, ...",
        },
        "source_span_id": {"type": "string", "format": "uuid"},
        "document_id": {"type": "string", "format": "uuid"},
        "char_start": {"type": "integer"},
        "char_end": {"type": "integer"},
        "entity_id": {"type": "string", "format": "uuid"},
        "confidence": {"type": "number"},
        "note": {"type": "string"},
    },
    "required": ["slug", "block_key", "relation"],
}


async def handler(
    container: Any,
    *,
    slug: str,
    block_key: str,
    relation: str,
    source_span_id: str | None = None,
    document_id: str | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
    entity_id: str | None = None,
    confidence: float | None = None,
    note: str | None = None,
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
    try:
        block_uuid = UUID(block_key)
        span_uuid = UUID(source_span_id) if source_span_id is not None else None
        doc_uuid = UUID(document_id) if document_id is not None else None
        entity_uuid = UUID(entity_id) if entity_id is not None else None
    except (ValueError, TypeError):
        return {
            "error": {
                "code": "invalid_input",
                "message": "block_key, source_span_id, document_id, and entity_id must be UUIDs",
                "details": None,
            }
        }
    try:
        written = await service.link(
            slug=slug,
            block_key=block_uuid,
            relation=relation,
            source_span_id=span_uuid,
            document_id=doc_uuid,
            char_start=char_start,
            char_end=char_end,
            entity_id=entity_uuid,
            confidence=confidence,
            note=note,
        )
        return written.model_dump(mode="json")
    except NotFoundError as exc:
        return {"error": {"code": "not_found", "message": str(exc), "details": None}}
    except FrozenRevisionError as exc:
        return {"error": {"code": "conflict", "message": str(exc), "details": None}}
    except ValueError as exc:
        return {"error": {"code": "invalid_input", "message": str(exc), "details": None}}
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_link_error", error=str(exc))
        return {"error": {"code": "work_link_failed", "message": str(exc), "details": None}}
