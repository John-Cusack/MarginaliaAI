"""get_passage_context tool -- expand around a passage."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger()

TOOL_NAME = "get_passage_context"
TOOL_DESCRIPTION = (
    "Expand around a passage with N passages before and after. "
    "Useful for reading surrounding context of a search hit."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passage_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the target passage.",
        },
        "before": {
            "type": "integer",
            "default": 2,
            "description": "Number of passages before the target to include.",
        },
        "after": {
            "type": "integer",
            "default": 2,
            "description": "Number of passages after the target to include.",
        },
    },
    "required": ["passage_id"],
}


async def handler(
    container: Any,
    *,
    passage_id: str,
    before: int = 2,
    after: int = 2,
) -> dict[str, Any]:
    """Expand context around a passage."""
    try:
        passage_repo = container.passage_repo

        pid = UUID(passage_id)
        before_passages, target, after_passages = await passage_repo.get_context(
            pid, before=before, after=after
        )

        return {
            "target": {"passage_id": str(target.id), "text": target.text},
            "before": [
                {"passage_id": str(p.id), "text": p.text}
                for p in before_passages
            ],
            "after": [
                {"passage_id": str(p.id), "text": p.text}
                for p in after_passages
            ],
            "document_id": str(target.document_id),
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("get_passage_context_error", error=str(e))
        return {"error": {"code": "get_passage_context_failed", "message": str(e), "details": None}}
