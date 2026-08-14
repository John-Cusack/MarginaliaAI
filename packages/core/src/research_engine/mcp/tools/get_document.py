"""get_document tool -- fetch a full document by ID."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger()

TOOL_NAME = "get_document"
TOOL_DESCRIPTION = (
    "Fetch a document by ID, including metadata and its passages. "
    "Optionally include the full concatenated text."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the document to retrieve.",
        },
        "include_full_text": {
            "type": "boolean",
            "default": False,
            "description": "Whether to include the full concatenated document text.",
        },
    },
    "required": ["document_id"],
}


async def handler(
    container: Any,
    *,
    document_id: str,
    include_full_text: bool = False,
) -> dict[str, Any]:
    """Fetch a document by ID with optional full text."""
    try:
        doc_repo = container.document_repo
        passage_repo = container.passage_repo

        doc_uuid = UUID(document_id)
        doc = await doc_repo.get(doc_uuid)
        if not doc:
            return {"error": {"code": "not_found", "message": f"Document not found: {document_id}", "details": None}}

        passages = await passage_repo.get_by_document(doc_uuid)
        passages_sorted = sorted(passages, key=lambda p: p.position)

        result: dict[str, Any] = {
            "id": str(doc.id),
            "title": doc.title,
            "document_type": doc.document_type,
            "metadata": doc.metadata,
            "passages": [
                {
                    "id": str(p.id),
                    "position": p.position,
                    "text": p.text,
                }
                for p in passages_sorted
            ],
        }

        if include_full_text:
            result["full_text"] = "\n\n".join(p.text for p in passages_sorted)

        return result
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("get_document_error", error=str(e))
        return {"error": {"code": "get_document_failed", "message": str(e), "details": None}}
