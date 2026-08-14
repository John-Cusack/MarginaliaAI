"""Built-in filter extensions for the hybrid search pipeline."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from research_engine.adapters.storage.postgres.schema import (
    event_actors,
    events,
    extraction_records,
)


class EventDateRangeFilter:
    """Filter passages linked to events occurring in a date range."""

    @property
    def filter_id(self) -> str:
        return "event_date_range"

    @property
    def description(self) -> str:
        return (
            "Filter to passages linked to events occurring in a date range. "
            "Use for historical queries like 'battles between 1805-1815'. "
            "Optionally restrict by event_type or actor_entity_ids."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "start": {
                    "type": "string",
                    "format": "date",
                    "description": "Start of date range (ISO 8601).",
                },
                "end": {
                    "type": "string",
                    "format": "date",
                    "description": "End of date range (ISO 8601).",
                },
                "event_type": {
                    "type": "string",
                    "description": "Optional event type filter.",
                },
                "actor_entity_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "description": "Optional: events involving these entities.",
                },
            },
            "required": ["start"],
        }

    def build_clause(self, value: Any) -> sa.sql.expression.SelectBase:
        conditions = [events.c.source_passage_id.isnot(None)]

        if value.get("start"):
            conditions.append(
                sa.or_(
                    events.c.timestamp_end >= value["start"],
                    sa.and_(events.c.timestamp_end.is_(None), events.c.timestamp_start >= value["start"]),
                )
            )
        if value.get("end"):
            conditions.append(events.c.timestamp_start <= value["end"])
        if value.get("event_type"):
            conditions.append(events.c.event_type == value["event_type"])

        stmt = sa.select(events.c.source_passage_id).where(sa.and_(*conditions))

        if actor_ids := value.get("actor_entity_ids"):
            stmt = stmt.join(event_actors, event_actors.c.event_id == events.c.id)
            stmt = stmt.where(event_actors.c.entity_id.in_(actor_ids))

        return stmt


class HasExtractionFilter:
    """Filter passages that have specific extraction records."""

    @property
    def filter_id(self) -> str:
        return "has_extraction"

    @property
    def description(self) -> str:
        return (
            "Filter to passages with specific extraction records "
            "(e.g. identified citations, cross-references, epistolary metadata). "
            "Requires record_type; optionally match on record data via JSONB containment."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "record_type": {
                    "type": "string",
                    "description": "The extraction record type to match.",
                },
                "data_contains": {
                    "type": "object",
                    "description": "JSONB containment match on record data.",
                },
            },
            "required": ["record_type"],
        }

    def build_clause(self, value: Any) -> sa.sql.expression.SelectBase:
        conditions = [extraction_records.c.record_type == value["record_type"]]
        if data_filter := value.get("data_contains"):
            conditions.append(extraction_records.c.data.contains(data_filter))
        return sa.select(extraction_records.c.passage_id).where(sa.and_(*conditions))
