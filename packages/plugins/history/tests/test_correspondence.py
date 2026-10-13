"""Cadence and missing letters, read from letter events and their actors.

The two failure modes these guard against: counting a man's letters to his
mother as letters to his friend (the actor filter is an OR), and filing every
letter whose sender nothing recorded under "b_to_a".
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from history.tools import find_missing_letters
from history.tools.correspondence_cadence import tool_handler as cadence

FARADAY = "11111111-1111-1111-1111-111111111111"
ABBOTT = "22222222-2222-2222-2222-222222222222"
MOTHER = "33333333-3333-3333-3333-333333333333"


def letter(event_id, when, actors, **payload):
    return SimpleNamespace(
        id=event_id,
        event_type="letter_sent",
        timestamp_start=datetime.fromisoformat(when).replace(tzinfo=UTC),
        payload=payload,
        actors=actors,
    )


def sender(entity):
    return {"entity_id": entity, "role": "sender"}


def recipient(entity):
    return {"entity_id": entity, "role": "recipient"}


class Events:
    def __init__(self, letters, with_actors=True):
        self.letters = letters
        if not with_actors:
            self.get_actors_many = None

    async def query(self, filters, k=1000, group_by=None):
        return self.letters, []

    async def get_actors_many(self, event_ids):
        return {str(e.id): e.actors for e in self.letters if str(e.id) in map(str, event_ids)}


TOUR = [
    letter("e1", "1814-09-06", [sender(FARADAY), recipient(ABBOTT)]),
    letter("e2", "1814-10-01", [sender(ABBOTT), recipient(FARADAY)]),
    letter("e3", "1814-11-10", [sender(FARADAY), recipient(MOTHER)]),
    letter("e4", "1814-11-26", [sender(FARADAY)]),  # recipient unresolved
    letter("e5", "1814-12-21", [{"entity_id": FARADAY, "role": "witness"},
                                {"entity_id": ABBOTT, "role": "witness"}]),
]


async def test_only_letters_between_the_two_are_counted():
    result = await cadence(Events(TOUR), FARADAY, ABBOTT)
    summary = result["summary"]
    assert (summary["a_to_b_count"], summary["b_to_a_count"]) == (1, 1)
    assert summary["unknown_direction_count"] == 1
    assert summary["total_letters"] == 3, "not the letter to his mother"
    assert summary["events_examined"] == 5
    assert any("unresolved other party" in note for note in result["notes"])


async def test_a_letter_naming_both_without_roles_is_unknown_not_b_to_a():
    result = await cadence(Events(TOUR), FARADAY, ABBOTT, time_bin="month")
    december = next(row for row in result["timeline"] if row["bin"] == "1814-12")
    assert (december["a_to_b"], december["b_to_a"], december["unknown"]) == (0, 0, 1)


async def test_an_older_core_falls_back_to_the_payload_copy():
    letters = [
        letter("e1", "1814-09-06", [], sender_entity_id=FARADAY, recipient_entity_id=ABBOTT),
        letter("e2", "1814-10-01", [], sender_entity_id=MOTHER, recipient_entity_id=FARADAY),
    ]
    result = await cadence(Events(letters, with_actors=False), FARADAY, ABBOTT)
    assert result["summary"]["a_to_b_count"] == 1
    assert result["summary"]["total_letters"] == 1
    assert any("payloads" in note for note in result["notes"])


class Extraction:
    def __init__(self, references):
        self.references = references

    async def query_records(self, record_type, filters=None, k=100, **kwargs):
        assert record_type == "epistolary_reference"
        return self.references


class Corpus:
    """Every passage sits in a letter document Faraday wrote."""

    async def get_passage_context(self, passage_id):
        return {"document_id": "letter-1"}

    async def get_document(self, document_id):
        return {"id": document_id, "metadata": {"sender_entity_id": FARADAY}}


def reference(when_as_written, start, kind="received_letter", confidence=0.9):
    return {
        "passage_id": "p1",
        "data": {
            "reference_type": kind,
            "referenced_party_surface": "Abbott",
            "referenced_date": when_as_written,
            "referenced_date_resolved": {"start": f"{start}T00:00:00+00:00"},
            "evidence": f"yours of {when_as_written}",
            "confidence": confidence,
        },
    }


async def missing(references, letters):
    return await find_missing_letters.tool_handler(
        Corpus(), Extraction(references), None, Events(letters),
        FARADAY, ABBOTT, method="referenced",
    )


async def test_a_reference_to_a_letter_held_that_day_is_held():
    """"yours of the 1st" in Faraday's letter: Abbott to Faraday, 1 October."""
    result = await missing([reference("the 1st", "1814-10-01")], TOUR)
    assert len(result["held"]) == 1
    assert result["held"][0]["expected_sender_entity_id"] == ABBOTT
    assert result["candidates"] == []


async def test_a_letter_one_day_off_is_a_near_miss_not_held():
    result = await missing([reference("the 2d", "1814-10-02")], TOUR)
    assert result["held"] == []
    assert result["candidates"] == []
    assert result["near_miss"][0]["near_miss_of"]["date"] == "1814-10-01"


async def test_the_wrong_direction_is_not_held():
    """Faraday did write to Abbott on 6 September — but this is Abbott's letter."""
    result = await missing([reference("the 6th", "1814-09-06")], TOUR)
    assert result["held"] == []
    assert len(result["candidates"]) == 1


async def test_an_own_letter_is_matched_the_other_way():
    result = await missing([reference("my last of the 6th", "1814-09-06", kind="prior_letter")], TOUR)
    assert len(result["held"]) == 1
    assert result["held"][0]["expected_sender_entity_id"] == FARADAY
