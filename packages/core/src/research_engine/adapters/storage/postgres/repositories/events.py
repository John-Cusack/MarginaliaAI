"""Postgres event repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import event_actors, events
from research_engine.domain.events import Event, EventActor, EventDraft, EventFilter

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGEventRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, tx: Transaction, draft: EventDraft) -> Event:
        event_id = uuid7()
        await tx.conn.execute(events.insert().values(id=event_id, **_values(draft)))
        return await self._get_by_id(tx.conn, event_id)  # type: ignore[return-value]

    async def upsert(
        self,
        tx: Transaction,
        draft: EventDraft,
        actors: list[EventActor] | None = None,
    ) -> Event:
        """Write the one event of this type derived from this passage.

        Events had no natural key, so a pass that materializes them could only
        insert, and re-running it doubled the timeline. ``(event_type,
        source_passage_id)`` is the key: a unit of evidence — a letter, say —
        yields one event of a given kind, and re-deriving it replaces the row in
        place, keeping its id. *actors*, when given, replace the event's actors
        wholesale; ``None`` leaves them as they are.
        """
        if draft.source_passage_id is None:
            raise ValueError(
                "An upserted event needs a source_passage_id: with event_type "
                "it is the key that makes re-deriving the event idempotent."
            )
        values = _values(draft)
        stmt = (
            pg_insert(events)
            .values(id=uuid7(), **values)
            .on_conflict_do_update(
                index_elements=[events.c.event_type, events.c.source_passage_id],
                set_=values,
            )
            .returning(events.c.id)
        )
        event_id = (await tx.conn.execute(stmt)).scalar_one()
        if actors is not None:
            await tx.conn.execute(
                event_actors.delete().where(event_actors.c.event_id == event_id)
            )
            for actor in actors:
                await self.add_actor(
                    tx,
                    EventActor(event_id=event_id, entity_id=actor.entity_id, role=actor.role),
                )
        return await self._get_by_id(tx.conn, event_id)  # type: ignore[return-value]

    async def delete(self, tx: Transaction, event_id: UUID) -> bool:
        """Remove an event and, by cascade, its actors. True if it existed."""
        result = await tx.conn.execute(events.delete().where(events.c.id == event_id))
        return bool(result.rowcount)

    async def get(self, event_id: UUID) -> Event | None:
        async with self._engine.connect() as conn:
            return await self._get_by_id(conn, event_id)

    async def query(self, filter: EventFilter, k: int) -> list[Event]:
        stmt = events.select()
        stmt = self._apply_filter(stmt, filter)
        stmt = stmt.limit(k)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
            return [self._to_domain(row) for row in rows]

    async def get_actors(self, event_id: UUID) -> list[EventActor]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    event_actors.select().where(event_actors.c.event_id == event_id)
                )
            ).all()
            return [
                EventActor(
                    event_id=row.event_id,
                    entity_id=row.entity_id,
                    role=row.role,
                )
                for row in rows
            ]

    async def get_actors_many(
        self, event_ids: list[UUID]
    ) -> dict[UUID, list[EventActor]]:
        """Actors of several events in one round trip, keyed by event id.

        Direction lives here, in each actor's role, and nowhere else; a caller
        reading a page of letters needs every sender and recipient at once, not
        one query per letter.
        """
        found: dict[UUID, list[EventActor]] = {event_id: [] for event_id in event_ids}
        if not event_ids:
            return found
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    event_actors.select().where(event_actors.c.event_id.in_(event_ids))
                )
            ).all()
        for row in rows:
            found.setdefault(row.event_id, []).append(
                EventActor(event_id=row.event_id, entity_id=row.entity_id, role=row.role)
            )
        return found

    async def add_actor(self, tx: Transaction, actor: EventActor) -> None:
        await tx.conn.execute(
            event_actors.insert().values(
                event_id=actor.event_id,
                entity_id=actor.entity_id,
                role=actor.role,
            )
        )

    async def count(self, filter: EventFilter | None = None) -> int:
        stmt = sa.select(sa.func.count()).select_from(events)
        if filter:
            stmt = self._apply_filter(stmt, filter)
        async with self._engine.connect() as conn:
            return (await conn.execute(stmt)).scalar_one()

    def _apply_filter(self, stmt: Any, f: EventFilter) -> Any:
        if f.event_types:
            stmt = stmt.where(events.c.event_type.in_(f.event_types))
        if f.location_id:
            stmt = stmt.where(events.c.location_id == f.location_id)
        # Use tstzrange overlap for time-based filtering
        if f.date_range_start and f.date_range_end:
            stmt = stmt.where(
                sa.func.tstzrange(
                    events.c.timestamp_start,
                    events.c.timestamp_end,
                    sa.literal_column("'[]'"),
                ).op("&&")(
                    sa.func.tstzrange(
                        f.date_range_start,
                        f.date_range_end,
                        sa.literal_column("'[]'"),
                    )
                )
            )
        elif f.date_range_start:
            stmt = stmt.where(
                sa.or_(
                    events.c.timestamp_end >= f.date_range_start,
                    sa.and_(
                        events.c.timestamp_end.is_(None),
                        events.c.timestamp_start >= f.date_range_start,
                    ),
                )
            )
        elif f.date_range_end:
            stmt = stmt.where(
                sa.or_(
                    events.c.timestamp_start <= f.date_range_end,
                    events.c.timestamp_start.is_(None),
                )
            )
        if f.actor_entity_ids:
            stmt = stmt.where(
                events.c.id.in_(
                    sa.select(event_actors.c.event_id).where(
                        event_actors.c.entity_id.in_(f.actor_entity_ids)
                    )
                )
            )
        if f.payload:
            # `payload` is a `json` column and `@>` is a `jsonb` operator: without
            # the casts Postgres rejects the query outright ("operator does not
            # exist: json @> json"), so every payload filter failed.
            stmt = stmt.where(
                sa.cast(events.c.payload, JSONB).contains(
                    sa.cast(sa.literal(f.payload, sa.JSON), JSONB)
                )
            )
        return stmt

    async def _get_by_id(self, conn: Any, event_id: UUID) -> Event | None:
        row = (
            await conn.execute(events.select().where(events.c.id == event_id))
        ).first()
        return self._to_domain(row) if row else None

    @staticmethod
    def _to_domain(row: Any) -> Event:
        return Event(
            id=row.id,
            event_type=row.event_type,
            timestamp_start=row.timestamp_start,
            timestamp_end=row.timestamp_end,
            precision=row.precision,
            location_id=row.location_id,
            location_text=row.location_text,
            source_passage_id=row.source_passage_id,
            payload=row.payload or {},
            confidence=row.confidence,
            created_at=row.created_at,
        )


def _values(draft: EventDraft) -> dict[str, Any]:
    """The columns an event draft writes, shared by insert and upsert."""
    return {
        "event_type": draft.event_type,
        "timestamp_start": draft.timestamp_start,
        "timestamp_end": draft.timestamp_end,
        "precision": draft.precision.value if draft.precision else None,
        "location_id": draft.location_id,
        "location_text": draft.location_text,
        "source_passage_id": draft.source_passage_id,
        "payload": draft.payload,
        "confidence": draft.confidence,
    }
