"""llm_usage tool -- what the corpus has spent on LLM calls, and where."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog

logger = structlog.get_logger()

TOOL_NAME = "llm_usage"
TOOL_DESCRIPTION = (
    "Report LLM spend and token usage over a time window, grouped by purpose, "
    "caller, and/or model. Costs are the estimates recorded at call time, not "
    "billed amounts — use them to compare operations, not to reconcile an "
    "invoice."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "days": {
            "type": "integer",
            "description": "Window size in days, counting back from now. Default 30.",
            "minimum": 1,
        },
        "group_by": {
            "type": "array",
            "description": "Columns to group by. Default ['purpose', 'caller', 'model'].",
            "items": {"type": "string", "enum": ["purpose", "caller", "model", "status"]},
        },
    },
}


async def handler(
    container: Any,
    *,
    days: int = 30,
    group_by: list[str] | None = None,
) -> dict[str, Any]:
    """Report LLM spend over a window."""
    try:
        since = container.clock.now() - timedelta(days=days)
        summary = await container.llm_calls_repo.usage_summary(
            since=since,
            group_by=tuple(group_by or ("purpose", "caller", "model")),
        )
        return {
            "window_days": days,
            "since": since.isoformat(),
            "group_by": summary.group_by,
            "total_calls": summary.total_calls,
            "total_cost_estimate": round(summary.total_cost, 4),
            "groups": [
                {
                    **g.key,
                    "calls": g.calls,
                    "input_tokens": g.input_tokens,
                    "output_tokens": g.output_tokens,
                    "cost_estimate": round(g.cost, 4),
                    "errors": g.errors,
                }
                for g in summary.groups
            ],
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("llm_usage_error", error=str(e))
        return {"error": {"code": "llm_usage_failed", "message": str(e), "details": None}}
