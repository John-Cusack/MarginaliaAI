"""events tool -- query events with filters, grouping, aggregation."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from research_engine.domain.events import EventFilter

logger = structlog.get_logger()

TOOL_NAME = "events"
TOOL_DESCRIPTION = (
    "Query the event store with filters, optional grouping by time period "
    "or event type, and aggregation. Returns both raw events and bucketed aggregates."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filters": {
            "type": "object",
            "description": "Filters for narrowing events.",
            "properties": {
                "event_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Event types to include.",
                },
                "actor_entity_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "description": "Filter by actor entity UUIDs.",
                },
                "date_range": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "ISO 8601 start."},
                        "end": {"type": "string", "description": "ISO 8601 end."},
                    },
                },
                "location_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Filter by location entity UUID.",
                },
                "payload": {
                    "type": "object",
                    "description": "Event-type-specific payload filters.",
                },
            },
        },
        "group_by": {
            "type": "string",
            "enum": ["month", "week", "day", "year", "event_type", "actor"],
            "description": "Group results into time buckets or by type.",
        },
        "aggregate": {
            "type": "string",
            "description": "Aggregation function to apply per bucket.",
        },
        "aggregate_field": {
            "type": "string",
            "description": "Payload field to aggregate on (e.g. 'payload.stance').",
        },
        "k": {
            "type": "integer",
            "default": 1000,
            "description": "Maximum number of events to return.",
        },
    },
}


def _parse_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


async def handler(
    container: Any,
    *,
    filters: dict[str, Any] | None = None,
    group_by: str | None = None,
    aggregate: str | None = None,
    aggregate_field: str | None = None,
    k: int = 1000,
) -> dict[str, Any]:
    """Query events with filters and optional grouping."""
    try:
        event_service = container.event_service

        filters = filters or {}
        date_range = filters.get("date_range", {})

        actor_ids = None
        if filters.get("actor_entity_ids"):
            actor_ids = [UUID(aid) for aid in filters["actor_entity_ids"]]

        event_filter = EventFilter(
            event_types=filters.get("event_types"),
            actor_entity_ids=actor_ids,
            date_range_start=_parse_datetime(date_range.get("start")),
            date_range_end=_parse_datetime(date_range.get("end")),
            location_id=UUID(filters["location_id"]) if filters.get("location_id") else None,
            payload=filters.get("payload"),
        )

        events, buckets = await event_service.query(
            filter=event_filter,
            k=k,
            group_by=group_by,
        )

        return {
            "events": [
                {
                    "id": str(e.id),
                    "event_type": e.event_type,
                    "timestamp_start": e.timestamp_start.isoformat() if e.timestamp_start else None,
                    "timestamp_end": e.timestamp_end.isoformat() if e.timestamp_end else None,
                    "precision": e.precision.value if e.precision else None,
                    "location_text": e.location_text,
                    "payload": e.payload,
                    "confidence": e.confidence,
                    "source_passage_id": str(e.source_passage_id) if e.source_passage_id else None,
                }
                for e in events
            ],
            "aggregates": [
                {
                    "bucket": b.bucket,
                    "count": b.count,
                    **b.aggregates,
                }
                for b in buckets
            ],
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("events_error", error=str(e))
        return {"error": {"code": "events_failed", "message": str(e), "details": None}}
