"""Derived events have a natural key, and their payload can be filtered.

A pass that turns letters into `letter_sent` events has to be re-runnable. With
no key but the id, a second run could only insert again and every letter would
appear twice on the timeline; `(event_type, source_passage_id)` is the key, and
re-deriving an event replaces it in place.

The payload filter is here too because it never worked at all: the column is
`json`, `@>` is a `jsonb` operator, and Postgres rejected every query that used
one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.events import PGEventRepo
from research_engine.adapters.storage.postgres.schema import entities, events
from research_engine.domain.events import EventActor, EventDraft, EventFilter
from research_engine.testing.corpus import new_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]


async def a_person(engine: AsyncEngine, corpus: Corpus, name: str):
    person_id = new_id()
    async with engine.begin() as conn:
        await conn.execute(
            entities.insert().values(
                id=person_id, entity_type="person", canonical_name=name, attributes={}
            )
        )
    return corpus.track(entities, person_id)


async def a_letter_passage(corpus: Corpus):
    document_id = await corpus.add_document(document_type="letter")
    return await corpus.add_passage(document_id, "FARADAY TO HIS MOTHER.")


def a_letter(passage_id, **overrides) -> EventDraft:
    fields = {
        "event_type": "letter_sent",
        "source_passage_id": passage_id,
        "location_text": "Geneva",
        "payload": {"letter_document_id": "doc-1", "date_as_written": "July 1"},
        "confidence": 0.7,
    }
    return EventDraft(**{**fields, **overrides})


async def test_re_deriving_an_event_replaces_it(engine: AsyncEngine, corpus: Corpus):
    repo = PGEventRepo(engine)
    passage_id = await a_letter_passage(corpus)

    async with transaction(engine) as tx:
        first = await repo.upsert(tx, a_letter(passage_id))
    corpus.track(events, first.id)
    async with transaction(engine) as tx:
        second = await repo.upsert(tx, a_letter(passage_id, confidence=0.9))

    assert second.id == first.id
    assert second.confidence == 0.9
    assert await repo.count(EventFilter(event_types=["letter_sent"], payload={
        "letter_document_id": "doc-1"
    })) == 1


async def test_actors_are_replaced_only_when_given(engine: AsyncEngine, corpus: Corpus):
    repo = PGEventRepo(engine)
    faraday = await a_person(engine, corpus, "Michael Faraday (test)")
    mother = await a_person(engine, corpus, "Margaret Faraday (test)")
    passage_id = await a_letter_passage(corpus)

    async with transaction(engine) as tx:
        event = await repo.upsert(
            tx,
            a_letter(passage_id),
            [EventActor(event_id=new_id(), entity_id=faraday, role="sender")],
        )
    corpus.track(events, event.id)
    async with transaction(engine) as tx:
        await repo.upsert(
            tx,
            a_letter(passage_id),
            [
                EventActor(event_id=new_id(), entity_id=faraday, role="sender"),
                EventActor(event_id=new_id(), entity_id=mother, role="recipient"),
            ],
        )
    async with transaction(engine) as tx:
        await repo.upsert(tx, a_letter(passage_id), None)

    actors = (await repo.get_actors_many([event.id]))[event.id]
    assert {(a.entity_id, a.role) for a in actors} == {
        (faraday, "sender"),
        (mother, "recipient"),
    }


async def test_a_plain_insert_cannot_duplicate_the_key(engine: AsyncEngine, corpus: Corpus):
    repo = PGEventRepo(engine)
    passage_id = await a_letter_passage(corpus)
    async with transaction(engine) as tx:
        event = await repo.insert(tx, a_letter(passage_id))
    corpus.track(events, event.id)

    with pytest.raises(sa.exc.IntegrityError):
        async with transaction(engine) as tx:
            await repo.insert(tx, a_letter(passage_id))


async def test_the_payload_filter_runs(engine: AsyncEngine, corpus: Corpus):
    repo = PGEventRepo(engine)
    passage_id = await a_letter_passage(corpus)
    async with transaction(engine) as tx:
        event = await repo.upsert(tx, a_letter(passage_id))
    corpus.track(events, event.id)

    found = await repo.query(EventFilter(payload={"date_as_written": "July 1"}), k=10)
    assert event.id in {e.id for e in found}
    assert not await repo.query(EventFilter(payload={"date_as_written": "July 2"}), k=10)


async def test_delete_reports_whether_it_existed(engine: AsyncEngine, corpus: Corpus):
    repo = PGEventRepo(engine)
    passage_id = await a_letter_passage(corpus)
    async with transaction(engine) as tx:
        event = await repo.upsert(tx, a_letter(passage_id))

    async with transaction(engine) as tx:
        assert await repo.delete(tx, event.id) is True
    async with transaction(engine) as tx:
        assert await repo.delete(tx, event.id) is False


async def test_an_upsert_without_a_passage_is_refused(engine: AsyncEngine):
    repo = PGEventRepo(engine)
    with pytest.raises(ValueError, match="source_passage_id"):
        async with transaction(engine) as tx:
            await repo.upsert(tx, EventDraft(event_type="letter_sent"))
