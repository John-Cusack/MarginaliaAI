"""corpus_stats tool -- corpus shape and coverage stats."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
import structlog

from research_engine.domain.documents import DocumentFilter

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

        # Extra coverage stats computed directly over core.documents, scoped by
        # the same filters. Guarded so non-DB-backed containers still work.
        engine = getattr(container, "engine", None)
        if engine is not None:
            result.update(await _coverage_stats(engine, filters or {}))

        return result
    except Exception as e:
        logger.error("corpus_stats_error", error=str(e))
        return {"error": {"code": "corpus_stats_failed", "message": str(e), "details": None}}


def _where(filters: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build a WHERE clause + params matching the DocumentFilter semantics."""
    conditions: list[str] = []
    params: dict[str, Any] = {}
    if dtypes := filters.get("document_types"):
        conditions.append("document_type = ANY(:dtypes)")
        params["dtypes"] = list(dtypes)
    if lang := filters.get("language"):
        conditions.append("language = :lang")
        params["lang"] = lang
    if date_start := filters.get("date_start"):
        conditions.append("created_date_start >= CAST(:dstart AS timestamptz)")
        params["dstart"] = date_start
    if date_end := filters.get("date_end"):
        conditions.append("created_date_end <= CAST(:dend AS timestamptz)")
        params["dend"] = date_end
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


async def _coverage_stats(engine: Any, filters: dict[str, Any]) -> dict[str, Any]:
    """by-author breakdown, date coverage, and language distribution."""
    where, params = _where(filters)

    lang_sql = sa.text(
        f"SELECT COALESCE(language, 'unknown') AS lang, COUNT(*) AS cnt "
        f"FROM core.documents {where} GROUP BY language ORDER BY cnt DESC"
    )
    date_sql = sa.text(
        f"SELECT MIN(created_date_start) AS earliest, MAX(created_date_end) AS latest, "
        f"COUNT(*) FILTER (WHERE created_date_start IS NOT NULL) AS dated, COUNT(*) AS total "
        f"FROM core.documents {where}"
    )
    # Authorship lives in metadata: a scalar 'author' and/or an 'authors' array.
    author_sql = sa.text(
        f"SELECT author, COUNT(*) AS cnt FROM ("
        f"  SELECT metadata->>'author' AS author FROM core.documents {where} "
        f"    {'AND' if where else 'WHERE'} metadata->>'author' IS NOT NULL "
        f"  UNION ALL "
        f"  SELECT json_array_elements_text(metadata->'authors') AS author FROM core.documents {where} "
        f"    {'AND' if where else 'WHERE'} json_typeof(metadata->'authors') = 'array' "
        f") t GROUP BY author ORDER BY cnt DESC LIMIT 50"
    )

    async with engine.connect() as conn:
        lang_rows = (await conn.execute(lang_sql, params)).all()
        date_row = (await conn.execute(date_sql, params)).first()
        author_rows = (await conn.execute(author_sql, params)).all()

    return {
        "by_language": {r.lang: r.cnt for r in lang_rows},
        "by_author": {r.author: r.cnt for r in author_rows},
        "date_coverage": {
            "earliest": str(date_row.earliest) if date_row and date_row.earliest else None,
            "latest": str(date_row.latest) if date_row and date_row.latest else None,
            "documents_with_dates": date_row.dated if date_row else 0,
            "total_documents": date_row.total if date_row else 0,
        },
    }
