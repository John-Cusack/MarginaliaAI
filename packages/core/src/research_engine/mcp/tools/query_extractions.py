"""query_extractions tool -- query cached extraction records."""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

TOOL_NAME = "query_extractions"
TOOL_DESCRIPTION = (
    "Retrieve previously-extracted records without re-running extraction. "
    "Query by record type, passage filter, and data-level filters."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "record_type": {
            "type": "string",
            "description": "The extraction record type to query (e.g. 'epistolary_reference').",
        },
        "passage_filter": {
            "type": "object",
            "description": "Filter to narrow which passages' records to return.",
        },
        "data_filter": {
            "type": "object",
            "description": "Filter on the extracted data fields (e.g. {'reference_type': 'prior_letter'}).",
        },
        "k": {
            "type": "integer",
            "default": 500,
            "description": "Maximum number of records to return.",
        },
    },
    "required": ["record_type"],
}


async def handler(
    container: Any,
    *,
    record_type: str,
    passage_filter: dict[str, Any] | None = None,
    data_filter: dict[str, Any] | None = None,
    k: int = 500,
) -> dict[str, Any]:
    """Query cached extraction records."""
    try:
        extraction_repo = container.extraction_repo

        # A passage filter selects passages, not record data. Folding both into
        # one dict and testing it against the record's `data` asked whether an
        # extracted claim contained a key named "passage_filter", which nothing
        # ever does — so any filtered query returned nothing at best.
        passage_ids = None
        if passage_filter:
            passage_ids = await container.passage_repo.filter_candidate_ids(
                {k_: v for k_, v in passage_filter.items() if v is not None},
                filter_extensions=container.registry.get_filter_extensions(),
            )

        records = await extraction_repo.query_records(
            record_type=record_type,
            data_filter=data_filter or None,
            passage_ids=passage_ids,
            k=k,
        )

        return {
            "records": [
                {
                    "id": str(r.id),
                    "extraction_id": str(r.extraction_id),
                    "passage_id": str(r.passage_id),
                    "schema_id": str(r.schema_id),
                    "record_type": r.record_type,
                    "data": r.data,
                    "evidence_start": r.evidence_start,
                    "evidence_end": r.evidence_end,
                    "created_at": r.created_at.isoformat(),
                }
                for r in records
            ],
            "total": len(records),
        }
    except Exception as e:
        logger.error("query_extractions_error", error=str(e))
        return {"error": {"code": "query_extractions_failed", "message": str(e), "details": None}}
