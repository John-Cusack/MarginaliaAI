"""find_passages tool -- hybrid search over the passage layer."""

from __future__ import annotations

import copy
from typing import Any

import structlog

from research_engine.domain.common import FusionMode
from research_engine.domain.passages import SearchFilters, SearchQuery

logger = structlog.get_logger()

TOOL_NAME = "find_passages"
TOOL_DESCRIPTION = (
    "Hybrid search over the passage layer. Supports keyword, vector, and "
    "fused retrieval with optional reranking. The workhorse tool for "
    "finding relevant passages across the corpus. Plugin filter extensions "
    "(e.g. scripture_ref_range, event_date_range) are available under "
    "filters.extensions when the providing plugin is loaded."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Natural language or keyword query.",
        },
        "filters": {
            "type": "object",
            "description": "Optional filters to narrow results.",
            "properties": {
                "document_type": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict to these document types.",
                },
                "author_entity_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Filter by author entity UUID.",
                },
                "recipient_entity_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Filter by recipient entity UUID.",
                },
                "date_range": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "ISO 8601 start date."},
                        "end": {"type": "string", "description": "ISO 8601 end date."},
                    },
                },
                "mentions_entity_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "description": "Passages mentioning all of these entities.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Document-type-specific metadata filters.",
                },
                "extensions": {
                    "type": "object",
                    "description": (
                        "Plugin filter extensions. Keys are extension IDs "
                        "(e.g. 'event_date_range', 'scripture_ref_range'). "
                        "Use list_available_filters to discover loaded extensions."
                    ),
                },
                "extension_logic": {
                    "type": "string",
                    "enum": ["and", "or"],
                    "default": "and",
                    "description": "How to combine extension filters: 'and' (intersection, default) or 'or' (union).",
                },
            },
        },
        "k": {
            "type": "integer",
            "default": 20,
            "description": "Number of results to return.",
        },
        "hybrid": {
            "type": "object",
            "description": "Hybrid search configuration.",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["rrf", "weighted", "vector_only", "keyword_only"],
                    "default": "rrf",
                    "description": "Fusion mode for combining vector and keyword results.",
                },
                "alpha": {
                    "type": "number",
                    "default": 0.5,
                    "description": "Weight for vector vs keyword in weighted mode (0=keyword, 1=vector).",
                },
                "rerank": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to rerank results with a cross-encoder.",
                },
            },
        },
    },
    "required": ["query"],
}

_MODE_MAP = {
    "rrf": FusionMode.rrf,
    "weighted": FusionMode.weighted,
    "vector_only": FusionMode.vector_only,
    "keyword_only": FusionMode.keyword_only,
}


def build_dynamic_schema(registry: Any) -> dict[str, Any]:
    """Return TOOL_SCHEMA with extension filters injected from the registry."""
    extensions = registry.get_filter_extensions()
    if not extensions:
        return TOOL_SCHEMA

    schema = copy.deepcopy(TOOL_SCHEMA)
    ext_schema: dict[str, Any] = {
        "type": "object",
        "description": (
            "Plugin filter extensions. Keys are extension IDs. "
            "Each extension narrows the candidate passage set."
        ),
        "properties": {},
    }
    for ext_id, ext_obj in extensions.items():
        ext_schema["properties"][ext_id] = {
            **ext_obj.input_schema,
            "description": ext_obj.description,
        }
    schema["properties"]["filters"]["properties"]["extensions"] = ext_schema
    return schema


async def handler(
    container: Any,
    *,
    query: str,
    filters: dict[str, Any] | None = None,
    k: int = 20,
    hybrid: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a hybrid passage search."""
    try:
        search_service = container.search_service

        # Build filters
        search_filters = None
        if filters:
            search_filters = SearchFilters(
                document_types=filters.get("document_type"),
                date_range_start=filters.get("date_range", {}).get("start") if filters.get("date_range") else None,
                date_range_end=filters.get("date_range", {}).get("end") if filters.get("date_range") else None,
                author_entity_id=filters.get("author_entity_id"),
                recipient_entity_id=filters.get("recipient_entity_id"),
                mentions_entity_ids=filters.get("mentions_entity_ids"),
                metadata=filters.get("metadata"),
                extensions=filters.get("extensions"),
                extension_logic=filters.get("extension_logic", "and"),
            )

        # Build hybrid config
        hybrid = hybrid or {}
        mode_str = hybrid.get("mode", "rrf")
        fusion_mode = _MODE_MAP.get(mode_str, FusionMode.rrf)
        alpha = hybrid.get("alpha", 0.5)
        rerank = hybrid.get("rerank", True)

        search_query = SearchQuery(
            text=query,
            filters=search_filters,
            k=k,
            fusion_mode=fusion_mode,
            alpha=alpha,
            rerank=rerank,
        )

        result = await search_service.find_passages(search_query)

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
                for h in result.hits
            ],
            "total_candidates": result.total_candidates,
            "applied_filters": result.applied_filters,
        }
    except Exception as e:
        logger.error("find_passages_error", error=str(e))
        return {"error": {"code": "find_passages_failed", "message": str(e), "details": None}}
