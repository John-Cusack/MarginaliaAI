"""timeline_compare tool -- overlay event streams on time axis."""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

TOOL_NAME = "timeline_compare"
TOOL_DESCRIPTION = (
    "Overlay multiple event streams on a shared time axis. Each stream "
    "has its own filter; results are aligned into matching time buckets "
    "for comparative analysis."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "streams": {
            "type": "array",
            "description": "Event streams to compare, each with a name and filters.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Label for this stream.",
                    },
                    "filters": {
                        "type": "object",
                        "description": "Event filters for this stream (same shape as events tool filters).",
                    },
                },
                "required": ["name"],
            },
        },
        "time_bin": {
            "type": "string",
            "enum": ["day", "week", "month"],
            "default": "month",
            "description": "Time granularity for bucketing.",
        },
    },
    "required": ["streams"],
}


async def handler(
    container: Any,
    *,
    streams: list[dict[str, Any]],
    time_bin: str = "month",
) -> dict[str, Any]:
    """Compare multiple event streams on a shared time axis."""
    try:
        event_service = container.event_service

        timeline_streams = await event_service.timeline_compare(
            streams=streams,
            time_bin=time_bin,
        )

        return {
            "streams": [
                {
                    "name": s.name,
                    "buckets": [
                        {
                            "bucket": b.bucket,
                            "count": b.count,
                            **b.aggregates,
                        }
                        for b in s.buckets
                    ],
                }
                for s in timeline_streams
            ],
            "time_bin": time_bin,
        }
    except Exception as e:
        logger.error("timeline_compare_error", error=str(e))
        return {"error": {"code": "timeline_compare_failed", "message": str(e), "details": None}}
