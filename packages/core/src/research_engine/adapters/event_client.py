"""SDK event client backed by the core event service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from research_engine.domain.events import (
    EventActor,
    EventDraft,
)
from research_engine.domain.events import (
    EventFilter as CoreEventFilter,
)
from research_engine_sdk import Event, EventFilter, TimelineBucket

if TYPE_CHECKING:
    from research_engine.services.events.service import EventService


class EventServiceAdapter:
    def __init__(self, service: EventService, transaction_factory: Any) -> None:
        self._service = service
        self._transaction = transaction_factory

    async def create(self, event: dict[str, Any]) -> Event:
        payload = dict(event)
        actor_values = payload.pop("actors", [])
        actors = [
            EventActor(
                event_id=UUID(int=0),
                entity_id=UUID(str(actor["entity_id"])),
                role=actor["role"],
            )
            for actor in actor_values
        ]
        async with self._transaction() as tx:
            created = await self._service.create(
                tx,
                EventDraft.model_validate(payload),
                actors=actors or None,
            )
        return Event.model_validate(created.model_dump())

    async def query(
        self,
        filters: EventFilter,
        k: int = 1000,
        group_by: str | None = None,
    ) -> tuple[list[Event], list[TimelineBucket]]:
        sdk_filter = EventFilter.model_validate(filters)
        events, buckets = await self._service.query(
            CoreEventFilter.model_validate(sdk_filter.model_dump()),
            k=k,
            group_by=group_by,
        )
        return (
            [Event.model_validate(event.model_dump()) for event in events],
            [TimelineBucket.model_validate(bucket.model_dump()) for bucket in buckets],
        )
