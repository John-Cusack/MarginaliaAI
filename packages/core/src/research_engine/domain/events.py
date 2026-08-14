"""Event and timeline domain types."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from research_engine.domain.common import DatePrecision


class FuzzyDate(BaseModel):
    """A date with explicit precision."""

    start: datetime
    end: datetime
    precision: DatePrecision


class Event(BaseModel):
    """An event in the timeline."""

    id: UUID
    event_type: str
    timestamp_start: datetime | None = None
    timestamp_end: datetime | None = None
    precision: DatePrecision | None = None
    location_id: UUID | None = None
    location_text: str | None = None
    source_passage_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    created_at: datetime


class EventDraft(BaseModel):
    """Data needed to create an event."""

    event_type: str
    timestamp_start: datetime | None = None
    timestamp_end: datetime | None = None
    precision: DatePrecision | None = None
    location_id: UUID | None = None
    location_text: str | None = None
    source_passage_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0


class EventActor(BaseModel):
    """An entity participating in an event."""

    event_id: UUID
    entity_id: UUID
    role: str


class EventFilter(BaseModel):
    """Filters for querying events."""

    event_types: list[str] | None = None
    actor_entity_ids: list[UUID] | None = None
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None
    location_id: UUID | None = None
    payload: dict[str, Any] | None = None


class TimelineBucket(BaseModel):
    """A time bucket in a timeline aggregation."""

    bucket: str
    count: int
    aggregates: dict[str, Any] = Field(default_factory=dict)


class TimelineStream(BaseModel):
    """A named stream in a timeline comparison."""

    name: str
    buckets: list[TimelineBucket]
