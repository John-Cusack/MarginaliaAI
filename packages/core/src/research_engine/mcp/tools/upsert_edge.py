"""upsert_edge tool -- create a relationship edge between nodes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from research_engine.domain.common import NodeKind
from research_engine.domain.edges import EdgeDraft

logger = structlog.get_logger()

TOOL_NAME = "upsert_edge"
TOOL_DESCRIPTION = (
    "Create a directed relationship edge between two nodes (entities, "
    "documents, passages, or events). Requires write capability grant."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_kind": {
            "type": "string",
            "enum": ["entity", "document", "passage", "event"],
            "description": "Kind of the source node.",
        },
        "source_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the source node.",
        },
        "target_kind": {
            "type": "string",
            "enum": ["entity", "document", "passage", "event"],
            "description": "Kind of the target node.",
        },
        "target_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the target node.",
        },
        "relation_type": {
            "type": "string",
            "description": "Type of relationship (e.g. 'replies_to', 'cites', 'references').",
        },
        "attributes": {
            "type": "object",
            "description": "Optional attributes for the edge.",
        },
        "source_passage_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the passage where this relationship was found.",
        },
        "confidence": {
            "type": "number",
            "default": 1.0,
            "description": "Confidence score (0.0 to 1.0).",
        },
    },
    "required": ["source_kind", "source_id", "target_kind", "target_id", "relation_type"],
}


async def handler(
    container: Any,
    *,
    source_kind: str,
    source_id: str,
    target_kind: str,
    target_id: str,
    relation_type: str,
    attributes: dict[str, Any] | None = None,
    source_passage_id: str | None = None,
    confidence: float = 1.0,
) -> dict[str, Any]:
    """Create a directed edge between two nodes."""
    try:
        edge_repo = container.edge_repo
        tx_factory = container.transaction_factory

        draft = EdgeDraft(
            source_kind=NodeKind(source_kind),
            source_id=UUID(source_id),
            target_kind=NodeKind(target_kind),
            target_id=UUID(target_id),
            relation_type=relation_type,
            attributes=attributes or {},
            source_passage_id=UUID(source_passage_id) if source_passage_id else None,
            confidence=confidence,
        )

        async with tx_factory() as tx:
            edge = await edge_repo.upsert(tx, draft)

        return {
            "id": str(edge.id),
            "source_kind": edge.source_kind.value,
            "source_id": str(edge.source_id),
            "target_kind": edge.target_kind.value,
            "target_id": str(edge.target_id),
            "relation_type": edge.relation_type,
            "attributes": edge.attributes,
            "confidence": edge.confidence,
            "created_at": edge.created_at.isoformat(),
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("upsert_edge_error", error=str(e))
        return {"error": {"code": "upsert_edge_failed", "message": str(e), "details": None}}
