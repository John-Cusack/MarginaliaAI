"""similar_to tool -- vector similarity to a known passage."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger()

TOOL_NAME = "similar_to"
TOOL_DESCRIPTION = (
    "Find passages similar to a known passage using vector similarity. "
    "A 'more like this' search. Output shape matches find_passages."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passage_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the passage to find similar passages for.",
        },
        "k": {
            "type": "integer",
            "default": 20,
            "description": "Number of similar passages to return.",
        },
        "filters": {
            "type": "object",
            "description": "Optional filters to narrow results (same shape as find_passages filters).",
        },
    },
    "required": ["passage_id"],
}


async def handler(
    container: Any,
    *,
    passage_id: str,
    k: int = 20,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Find passages similar to a given passage."""
    try:
        search_service = container.search_service
        passage_repo = container.passage_repo

        pid = UUID(passage_id)

        # If filters provided, resolve candidate IDs
        candidate_ids: list[UUID] | None = None
        filters_applied: dict[str, Any] = {}
        if filters:
            filter_dict = {k_: v for k_, v in filters.items() if v is not None}
            if filter_dict:
                candidate_ids = await passage_repo.filter_candidate_ids(
                    filter_dict,
                    filter_extensions=container.registry.get_filter_extensions(),
                )
                filters_applied = filter_dict

        hits = await search_service.similar_to(pid, k=k, candidate_ids=candidate_ids)

        return {
            "hits": [
                {
                    "passage_id": str(h.passage_id),
                    "document_id": str(h.document_id),
                    "score": h.score,
                    "score_breakdown": h.score_breakdown.model_dump(exclude_none=True) if h.score_breakdown else {},
                    "text": h.text,
                    "metadata": h.metadata,
                    "locator": h.locator,
                    "context_available": h.context_available,
                }
                for h in hits
            ],
            "total_candidates": len(candidate_ids) if candidate_ids else 0,
            "applied_filters": filters_applied,
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("similar_to_error", error=str(e))
        return {"error": {"code": "similar_to_failed", "message": str(e), "details": None}}
