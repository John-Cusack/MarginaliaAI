"""citations tool -- read the citation graph (edges) for a document.

Counterpart to the write path (``upsert_edge`` / the EdgeClient). Without this,
the graph would be write-only and invisible to the agent.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger()

TOOL_NAME = "citations"
TOOL_DESCRIPTION = (
    "Read the citation graph for a document. direction='cites' returns the "
    "documents this document cites (outgoing edges); direction='cited_by' "
    "returns documents that cite this one (incoming edges). Defaults to the "
    "'cites' relation type."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the document whose edges to read.",
        },
        "direction": {
            "type": "string",
            "enum": ["cites", "cited_by"],
            "default": "cites",
            "description": "'cites' = outgoing edges; 'cited_by' = incoming edges.",
        },
        "relation_type": {
            "type": "string",
            "default": "cites",
            "description": "Edge relation_type to filter on (default 'cites').",
        },
    },
    "required": ["document_id"],
}


async def handler(
    container: Any,
    *,
    document_id: str,
    direction: str = "cites",
    relation_type: str | None = "cites",
) -> dict[str, Any]:
    """Read citation edges incident to a document, hydrating the other end."""
    edge_repo = container.edge_repo
    document_repo = container.document_repo
    doc_uuid = UUID(document_id)

    if direction == "cited_by":
        edges = await edge_repo.query_by_target("document", doc_uuid, relation_type)
        neighbor = lambda e: (e.source_kind, e.source_id)  # noqa: E731
    else:
        edges = await edge_repo.query_by_source("document", doc_uuid, relation_type)
        neighbor = lambda e: (e.target_kind, e.target_id)  # noqa: E731

    results: list[dict[str, Any]] = []
    for edge in edges:
        kind, other_id = neighbor(edge)
        title = None
        if (kind.value if hasattr(kind, "value") else kind) == "document":
            doc = await document_repo.get(other_id)
            title = doc.title if doc else None
        results.append(
            {
                "edge_id": str(edge.id),
                "relation_type": edge.relation_type,
                "document_id": str(other_id),
                "title": title,
                "confidence": edge.confidence,
                "attributes": edge.attributes,
            }
        )

    return {
        "document_id": document_id,
        "direction": direction,
        "relation_type": relation_type,
        "count": len(results),
        "edges": results,
    }
