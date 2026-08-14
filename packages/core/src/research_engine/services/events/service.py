"""Event service with fuzzy-date queries and timeline comparison."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

import structlog

from research_engine.domain.events import (
    Event,
    EventActor,
    EventDraft,
    EventFilter,
    TimelineBucket,
    TimelineStream,
)

if TYPE_CHECKING:
    from uuid import UUID

    from research_engine.ports.repositories import EventRepo, Transaction

logger = structlog.get_logger()


class EventService:
    def __init__(self, events: EventRepo) -> None:
        self._events = events

    async def create(self, tx: Transaction, draft: EventDraft, actors: list[EventActor] | None = None) -> Event:
        event = await self._events.insert(tx, draft)
        if actors:
            for actor in actors:
                await self._events.add_actor(
                    tx, EventActor(event_id=event.id, entity_id=actor.entity_id, role=actor.role)
                )
        return event

    async def get(self, event_id: UUID) -> Event | None:
        return await self._events.get(event_id)

    async def query(
        self,
        filter: EventFilter,
        k: int = 1000,
        group_by: str | None = None,
    ) -> tuple[list[Event], list[TimelineBucket]]:
        events = await self._events.query(filter, k)

        buckets = []
        if group_by:
            buckets = self._group_events(events, group_by)

        return events, buckets

    async def timeline_compare(
        self,
        streams: list[dict[str, Any]],
        time_bin: str = "month",
    ) -> list[TimelineStream]:
        result = []
        for stream_def in streams:
            name = stream_def["name"]
            filter_data = stream_def.get("filters", {})
            event_filter = EventFilter(**filter_data)
            events = await self._events.query(event_filter, k=10000)
            buckets = self._group_events(events, time_bin)
            result.append(TimelineStream(name=name, buckets=buckets))
        return result

    async def get_actors(self, event_id: UUID) -> list[EventActor]:
        return await self._events.get_actors(event_id)

    def _group_events(self, events: list[Event], group_by: str) -> list[TimelineBucket]:
        groups: dict[str, list[Event]] = defaultdict(list)

        for event in events:
            key = self._bucket_key(event, group_by)
            if key:
                groups[key].append(event)

        buckets = []
        for key in sorted(groups.keys()):
            group = groups[key]
            buckets.append(
                TimelineBucket(
                    bucket=key,
                    count=len(group),
                    aggregates={"event_types": list({e.event_type for e in group})},
                )
            )
        return buckets

    @staticmethod
    def _bucket_key(event: Event, group_by: str) -> str | None:
        ts = event.timestamp_start
        if not ts:
            return None

        if group_by == "day":
            return ts.strftime("%Y-%m-%d")
        elif group_by == "week":
            return ts.strftime("%Y-W%W")
        elif group_by == "month":
            return ts.strftime("%Y-%m")
        elif group_by == "year":
            return ts.strftime("%Y")
        elif group_by == "event_type":
            return event.event_type
        return ts.strftime("%Y-%m")
