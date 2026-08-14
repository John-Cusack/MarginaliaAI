"""upsert_event tool -- create an event record."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from research_engine.domain.events import EventActor, EventDraft

logger = structlog.get_logger()

TOOL_NAME = "upsert_event"
TOOL_DESCRIPTION = (
    "Create an event record in the event store. Events can be linked to "
    "actors (entities) and source passages. Requires write capability grant."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "event_type": {
            "type": "string",
            "description": "Type of event (e.g. 'letter_sent', 'battle', 'meeting').",
        },
        "timestamp_start": {
            "type": "string",
            "description": "ISO 8601 start timestamp.",
        },
        "timestamp_end": {
            "type": "string",
            "description": "ISO 8601 end timestamp.",
        },
        "precision": {
            "type": "string",
            "enum": ["day", "week", "month", "season", "year", "decade"],
            "description": "Temporal precision of the event date.",
        },
        "location_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the location entity.",
        },
        "location_text": {
            "type": "string",
            "description": "Free-text location description.",
        },
        "source_passage_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the passage this event was derived from.",
        },
        "payload": {
            "type": "object",
            "description": "Event-type-specific payload data.",
        },
        "confidence": {
            "type": "number",
            "default": 1.0,
            "description": "Confidence score (0.0 to 1.0).",
        },
        "actors": {
            "type": "array",
            "description": "Entities involved in this event.",
            "items": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "format": "uuid"},
                    "role": {"type": "string", "description": "Role in the event (e.g. 'sender', 'recipient')."},
                },
                "required": ["entity_id", "role"],
            },
        },
    },
    "required": ["event_type"],
}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


async def handler(
    container: Any,
    *,
    event_type: str,
    timestamp_start: str | None = None,
    timestamp_end: str | None = None,
    precision: str | None = None,
    location_id: str | None = None,
    location_text: str | None = None,
    source_passage_id: str | None = None,
    payload: dict[str, Any] | None = None,
    confidence: float = 1.0,
    actors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create an event record."""
    try:
        event_service = container.event_service
        tx_factory = container.transaction_factory

        draft = EventDraft(
            event_type=event_type,
            timestamp_start=_parse_dt(timestamp_start),
            timestamp_end=_parse_dt(timestamp_end),
            precision=precision,
            location_id=UUID(location_id) if location_id else None,
            location_text=location_text,
            source_passage_id=UUID(source_passage_id) if source_passage_id else None,
            payload=payload or {},
            confidence=confidence,
        )

        actor_objs = None
        if actors:
            # EventActor needs event_id but we don't have it yet;
            # the service will set it after insert
            actor_objs = [
                EventActor(
                    event_id=UUID("00000000-0000-0000-0000-000000000000"),  # placeholder
                    entity_id=UUID(a["entity_id"]),
                    role=a["role"],
                )
                for a in actors
            ]

        async with tx_factory() as tx:
            event = await event_service.create(tx, draft, actors=actor_objs)

        return {
            "id": str(event.id),
            "event_type": event.event_type,
            "timestamp_start": event.timestamp_start.isoformat() if event.timestamp_start else None,
            "timestamp_end": event.timestamp_end.isoformat() if event.timestamp_end else None,
            "precision": event.precision.value if event.precision else None,
            "payload": event.payload,
            "confidence": event.confidence,
            "created_at": event.created_at.isoformat(),
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("upsert_event_error", error=str(e))
        return {"error": {"code": "upsert_event_failed", "message": str(e), "details": None}}
