"""get_document_outline tool -- the map you read before opening anything."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger()

TOOL_NAME = "get_document_outline"
TOOL_DESCRIPTION = (
    "Read a document's structure: its parts, chapters and sections with their "
    "titles and sizes, without any of the prose. Start here for questions about "
    "where something is discussed, what a document covers, or what a named part "
    "of it argues — then open the node you want with read_node. Depth-limit it: "
    "a long book's full structure is thousands of entries, its chapters a few "
    "dozen."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the document.",
        },
        "max_depth": {
            "type": "integer",
            "default": 2,
            "description": (
                "How far down to descend. 1 is chapters only, 2 adds their "
                "sections. Omit the limit only when you need the whole tree."
            ),
        },
    },
    "required": ["document_id"],
}


async def handler(
    container: Any,
    *,
    document_id: str,
    max_depth: int | None = 2,
) -> dict[str, Any]:
    """Return the document's node tree, depth-limited."""
    try:
        doc_id = UUID(document_id)
        nodes = await container.document_nodes.get_outline(doc_id, max_depth=max_depth)

        if not nodes:
            # Absence is not emptiness: a document ingested before structure
            # existed, or parsed by a module that reports none, looks identical
            # to one genuinely without sections. Say which so the caller falls
            # back to find_passages rather than concluding the document is bare.
            return {
                "document_id": document_id,
                "nodes": [],
                "note": (
                    "No structure recorded for this document. It was ingested "
                    "before document structure was captured, or its parser "
                    "reports none. Use find_passages instead."
                ),
            }

        return {
            "document_id": document_id,
            "node_count": len(nodes),
            "truncated_at_depth": max_depth,
            "nodes": [
                {
                    "node_id": str(node.id),
                    "title": node.title,
                    "node_type": node.node_type,
                    "depth": node.depth,
                    "path": node.path,
                    # Character length, not token count: it is exact, and it is
                    # what tells a caller whether read_node will be cheap.
                    "char_length": node.char_end - node.char_start,
                }
                for node in nodes
            ],
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("get_document_outline_error", error=str(e))
        return {
            "error": {
                "code": "get_document_outline_failed",
                "message": str(e),
                "details": None,
            }
        }
