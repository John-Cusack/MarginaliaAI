"""Coverage stats over `core.documents` for the `corpus_stats` tool.

These three queries used to live in the MCP tool module, where nothing else
could reuse or test them. They read here like every other repository: one
class over the engine, returning plain dicts the tool merges into its reply.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class PGDiagnosticsRepo:
    """Author, date-coverage, and language breakdowns of the corpus."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def coverage_stats(self, filters: dict[str, Any]) -> dict[str, Any]:
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

        async with self._engine.connect() as conn:
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
