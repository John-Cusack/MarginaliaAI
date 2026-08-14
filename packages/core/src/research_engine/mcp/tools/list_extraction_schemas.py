"""list_extraction_schemas tool -- list available extraction schemas."""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

TOOL_NAME = "list_extraction_schemas"
TOOL_DESCRIPTION = (
    "List all registered extraction schemas. Discovery tool to learn "
    "what schemas are available before running extractions."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}


async def handler(
    container: Any,
) -> dict[str, Any]:
    """List all available extraction schemas."""
    try:
        schema_repo = container.extraction_schema_repo

        schemas = await schema_repo.list_all()

        return {
            "schemas": [
                {
                    "name": s.name,
                    "version": s.version,
                    "owner": s.owner,
                    "description": s.prompt_template[:200] if s.prompt_template else "",
                    "schema": s.schema_def,
                }
                for s in schemas
            ],
        }
    except Exception as e:
        logger.error("list_extraction_schemas_error", error=str(e))
        return {"error": {"code": "list_extraction_schemas_failed", "message": str(e), "details": None}}
