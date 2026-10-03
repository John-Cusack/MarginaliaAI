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
        draft, actors = _split(event)
        async with self._transaction() as tx:
            created = await self._service.create(tx, draft, actors=actors or None)
        return Event.model_validate(created.model_dump())

    async def upsert(self, event: dict[str, Any]) -> Event:
        """Create or replace the event of this type derived from this passage.

        Keyed on ``(event_type, source_passage_id)``, so ``source_passage_id`` is
        required. ``actors``, when present, replace the stored actors; leave the
        key out to keep them.
        """
        draft, actors = _split(event)
        async with self._transaction() as tx:
            stored = await self._service.upsert(
                tx, draft, actors=actors if "actors" in event else None
            )
        return Event.model_validate(stored.model_dump())

    async def delete(self, event_id: UUID | str) -> bool:
        async with self._transaction() as tx:
            return await self._service.delete(tx, UUID(str(event_id)))

    async def get_actors(self, event_id: UUID | str) -> list[dict[str, Any]]:
        actors = await self._service.get_actors(UUID(str(event_id)))
        return [_actor_json(actor) for actor in actors]

    async def get_actors_many(
        self, event_ids: list[UUID | str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Actors of several events, keyed by event id as a string."""
        found = await self._service.get_actors_many([UUID(str(eid)) for eid in event_ids])
        return {
            str(event_id): [_actor_json(actor) for actor in actors]
            for event_id, actors in found.items()
        }

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


def _split(event: dict[str, Any]) -> tuple[EventDraft, list[EventActor]]:
    """An SDK event dict as a core draft and its actors."""
    payload = dict(event)
    actor_values = payload.pop("actors", None) or []
    actors = [
        EventActor(
            event_id=UUID(int=0),
            entity_id=UUID(str(actor["entity_id"])),
            role=actor["role"],
        )
        for actor in actor_values
    ]
    return EventDraft.model_validate(payload), actors


def _actor_json(actor: EventActor) -> dict[str, Any]:
    return {"entity_id": str(actor.entity_id), "role": actor.role}
