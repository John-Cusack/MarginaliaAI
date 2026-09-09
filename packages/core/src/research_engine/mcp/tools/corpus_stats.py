"""corpus_stats tool -- corpus shape and coverage stats."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.domain.documents import DocumentFilter
from research_engine.mcp.errors import failed

logger = structlog.get_logger()

TOOL_NAME = "corpus_stats"
TOOL_DESCRIPTION = (
    "Return corpus shape and coverage statistics to help plan research. "
    "Includes document counts, passage counts, breakdowns by type and "
    "author, date coverage, and language distribution."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filters": {
            "type": "object",
            "description": "Optional filters to scope the stats.",
            "properties": {
                "document_types": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "date_start": {"type": "string"},
                "date_end": {"type": "string"},
                "language": {"type": "string"},
            },
        },
    },
}


async def handler(
    container: Any,
    *,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return corpus shape and coverage statistics."""
    try:
        document_repo = container.document_repo
        passage_repo = container.passage_repo
        entity_repo = container.entity_repo
        event_repo = container.event_repo

        # Build document filter
        doc_filter = None
        if filters:
            doc_filter = DocumentFilter(
                document_types=filters.get("document_types"),
                date_start=filters.get("date_start"),
                date_end=filters.get("date_end"),
                language=filters.get("language"),
            )

        document_count = await document_repo.count(doc_filter)
        passage_count = await passage_repo.count()
        entity_count = await entity_repo.count()
        event_count = await event_repo.count()

        # Build type breakdown by iterating document types
        by_document_type: dict[str, int] = {}
        registry = container.registry
        for dt in registry.list_document_types():
            dt_filter = DocumentFilter(document_types=[dt])
            count = await document_repo.count(dt_filter)
            if count > 0:
                by_document_type[dt] = count

        result: dict[str, Any] = {
            "document_count": document_count,
            "passage_count": passage_count,
            "entity_count": entity_count,
            "event_count": event_count,
            "by_document_type": by_document_type,
        }

        # Extra coverage stats over core.documents, scoped by the same
        # filters. Guarded so non-DB-backed containers still work.
        diagnostics = getattr(container, "diagnostics_repo", None)
        if diagnostics is not None:
            result.update(await diagnostics.coverage_stats(filters or {}))

        return result
    except Exception as e:
        logger.error("corpus_stats_error", error=str(e))
        return failed(TOOL_NAME, e)
