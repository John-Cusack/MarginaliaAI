"""extract tool -- run extraction schema against passages."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from research_engine.domain.extractions import ExtractionOptions

logger = structlog.get_logger()

TOOL_NAME = "extract"
TOOL_DESCRIPTION = (
    "Run a registered or ad-hoc extraction schema against passages. "
    "Extractions are cached; use force_refresh to re-extract. "
    "Can accept explicit passage IDs or a passage filter."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passage_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "description": "Explicit passage UUIDs to extract from.",
        },
        "passage_filter": {
            "type": "object",
            "description": "Filter to select passages (same shape as find_passages filters). Used if passage_ids is not provided.",
        },
        "schema": {
            "type": "string",
            "description": "Schema reference as 'name:version' (e.g. 'epistolary_references:2') or an inline schema object.",
        },
        "options": {
            "type": "object",
            "description": "Extraction options.",
            "properties": {
                "force_refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Bypass cache and re-extract.",
                },
                "llm_model": {
                    "type": "string",
                    "description": "Override the default LLM model.",
                },
                "concurrency": {
                    "type": "integer",
                    "default": 8,
                    "description": "Max concurrent extraction calls.",
                },
            },
        },
    },
    "required": ["schema"],
}


async def handler(
    container: Any,
    *,
    schema: str,
    passage_ids: list[str] | None = None,
    passage_filter: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run extraction on passages."""
    try:
        extraction_executor = container.extraction_executor
        passage_repo = container.passage_repo

        # Resolve passage IDs
        pids: list[UUID]
        if passage_ids:
            pids = [UUID(pid) for pid in passage_ids]
        elif passage_filter:
            filter_dict = {k: v for k, v in passage_filter.items() if v is not None}
            candidate_ids = await passage_repo.filter_candidate_ids(
                filter_dict,
                filter_extensions=container.registry.get_filter_extensions(),
            )
            pids = candidate_ids
        else:
            return {
                "error": {
                    "code": "invalid_input",
                    "message": "Either passage_ids or passage_filter is required.",
                    "details": None,
                }
            }

        if not pids:
            return {"extractions": [], "message": "No passages matched the filter."}

        # Build options
        opts = ExtractionOptions()
        if options:
            opts = ExtractionOptions(
                force_refresh=options.get("force_refresh", False),
                llm_model=options.get("llm_model"),
                concurrency=options.get("concurrency", 8),
            )

        batch = await extraction_executor.execute(
            passage_ids=pids,
            schema_ref=schema,
            options=opts,
        )

        return {
            "extractions": [
                {
                    "passage_id": str(r.passage_id),
                    "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                    "records": r.records,
                    "from_cache": r.from_cache,
                    "llm_call_id": str(r.llm_call_id) if r.llm_call_id else None,
                    "error": r.error,
                }
                for r in batch.results
            ],
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("extract_error", error=str(e))
        return {"error": {"code": "extract_failed", "message": str(e), "details": None}}
