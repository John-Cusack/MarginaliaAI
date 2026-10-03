"""`history.structure_letters` end to end, against in-memory clients.

The volume is a stretch of Faraday's continental tour as Bence Jones prints it:
a chapter opening, a dated letter, a journal entry, a letter with no year and a
receipt note, a letter dated "1816" between letters of 1814 and 1815, and the
publisher's catalogue at the back.
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from history.tools.structure_letters import tool_handler
from letter_fixtures import (
    BRUSSELS_1815,
    GENEVA_AUGUST_6,
    GENEVA_JULY_1,
    ROME_1816,
    ROME_APRIL_1814,
    ROME_FEBRUARY_1815,
)

FARADAY = "11111111-1111-1111-1111-111111111111"
MOTHER = "22222222-2222-2222-2222-222222222222"
VOLUME = "33333333-3333-3333-3333-333333333333"

PIECES = [
    ("chapter", "CHAPTER III. EXTRACTS FROM HIS JOURNAL AND LETTERS WHILST ABROAD.\n\n"
     "The journey of Faraday abroad with Sir H. Davy was one of the few episodes.\n\n"),
    ("rome", "FARADAT TO HIS MOTHER.\n\n' Eome : April 14, 1814.\n\n"
     "' My dear It is with singular pleasure I commence writing after so long a silence.\n\n"
     "-Milan.\n\nFriday, 17th. Saw M. Volta, who came to Sir H. Davy.\n\n"),
    ("geneva", "FARADAY TO HIS MOTHER.\n\n' Geneva : July 1. Received July 18.\n\n"
     "' I hope, dear Mother, that you are in good health.\n\n'M. Faeadat.'\n\n"),
    ("abbott", "TO ME. E. G. ABBOTT.\n\n' Geueva : Saturday, August 6, 1814. Received August 18.\n\n"
     "' Dear Eobert, too grateful for the goodness of Mr. De la Eoche.\n\n"),
    ("rome1816", "FARADAY TO HIS MOTHER.\n\n' Rome : February 13, 1816.\n\n"
     "' My dear Mother, put the letter in the post.\n\n"),
    ("huxtable", "FAEADAY TO IIUXTABLE.\n\n' Eome : February 13, 1815.\n\n"
     "' Dear Huxtable, unmanned, unguided skiff.\n\n"),
    ("brussels", "FARADAY TO HIS MOTHER.\n\n' Bruxelles : April 16, 1815.\n\n"
     "' Dear Mother, we are in Brussels.\n\n"),
    ("catalogue", "WORKS of UTILITY and GENERAL INFORMATION.\n\n"
     "FARADAY TO HIS MOTHER. Longmans catalogue, 8vo. price 10s.\n"),
]
TEXT = "".join(piece for _, piece in PIECES)
AT = dict(zip(
    [name for name, _ in PIECES],
    itertools.accumulate([0] + [len(piece) for _, piece in PIECES[:-1]]),
    strict=True,
))
READINGS = {
    "rome": ROME_APRIL_1814,
    "geneva": GENEVA_JULY_1,
    "abbott": GENEVA_AUGUST_6,
    "rome1816": ROME_1816,
    "huxtable": ROME_FEBRUARY_1815,
    "brussels": BRUSSELS_1815,
    # The model found a "letter" in the catalogue; the excluded range drops it.
    "catalogue": {**BRUSSELS_1815, "opening": "FARADAY TO HIS MOTHER."},
}


class World:
    """One in-memory corpus behind all four clients, as in the engine."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.texts: dict[str, str] = {}
        self.events: dict[str, SimpleNamespace] = {}
        self.ids = itertools.count(1)
        self.ingests = 0
        self.fail_on_ingest: int | None = None
        self.add_volume()

    def new_id(self) -> str:
        return f"00000000-0000-0000-0000-{next(self.ids):012d}"

    def add_volume(self, document_type: str = "letter_collection") -> None:
        passages = [
            {"id": f"vp-{name}", "position": i, "text": TEXT[AT[name]:], "char_start": AT[name],
             "char_end": len(TEXT), "node_id": None}
            for i, (name, _) in enumerate(PIECES)
        ]
        self.documents[VOLUME] = {
            "id": VOLUME,
            "title": "The life and letters of Faraday",
            "document_type": document_type,
            "source": "/books/faraday-vol-1.pdf",
            "language": "en",
            "edition_id": None,
            "metadata": {
                "default_sender": {"entity_id": FARADAY, "name": "Michael Faraday"},
                "alias_map": {"his mother": {"entity_id": MOTHER, "name": "Margaret Faraday"}},
                "excluded_ranges": [[AT["catalogue"], len(TEXT)]],
            },
            "passages": passages,
        }
        self.texts[VOLUME] = TEXT


class Corpus:
    def __init__(self, world: World) -> None:
        self.w = world

    async def get_document(self, document_id):
        doc = self.w.documents.get(str(document_id))
        return None if doc is None else {**doc, "metadata": dict(doc["metadata"])}

    async def get_document_text(self, document_id):
        return self.w.texts.get(str(document_id))

    async def get_document_outline(self, document_id, dated_only=False):
        return [
            {"title": "CHAPTER III. EXTRACTS", "char_start": 0, "char_end": len(TEXT), "depth": 1},
            {"title": "WORKS of UTILITY", "char_start": AT["catalogue"], "char_end": len(TEXT), "depth": 1},
        ]

    async def find_documents(self, *, document_types=None, metadata=None, source_pattern=None, limit=1000):
        return [
            doc for doc in self.w.documents.values()
            if (not document_types or doc["document_type"] in document_types)
            and all(doc["metadata"].get(k) == v for k, v in (metadata or {}).items())
        ][:limit]


class Extraction:
    def __init__(self, world: World, readings: dict[str, dict]) -> None:
        # evidence_start is where the opening sits in its passage, as the
        # extraction validator locates it.
        self.records = [
            {
                "id": f"rec-{name}",
                "passage_id": f"vp-{name}",
                "evidence_start": TEXT[AT[name]:].index(data["opening"]),
                "data": data,
            }
            for name, data in readings.items()
        ]

    async def query_records(self, record_type, filters=None, k=100, *, passage_ids=None, schema=None):
        assert record_type == "letter_opening" and schema == "letter_openings:1"
        return [r for r in self.records if passage_ids is None or r["passage_id"] in passage_ids][:k]


class Ingestion:
    def __init__(self, world: World) -> None:
        self.w = world

    async def ingest_document(self, *, title, document_type, text, source, metadata=None,
                              language=None, edition_id=None, created_date_start=None,
                              created_date_end=None, created_precision=None, sections=None):
        self.w.ingests += 1
        if self.w.fail_on_ingest is not None and self.w.ingests == self.w.fail_on_ingest:
            raise RuntimeError("EmbeddingUnavailable: the GPU host is asleep")
        digest = hashlib.sha256(text.encode()).hexdigest()
        for doc in self.w.documents.values():
            if doc["source"] == source and doc.get("hash") == digest:
                return {"document_id": doc["id"], "passage_count": 1, "skipped": "duplicate"}
        doc_id = self.w.new_id()
        self.w.documents[doc_id] = {
            "id": doc_id, "title": title, "document_type": document_type, "source": source,
            "hash": digest, "language": language, "metadata": dict(metadata or {}),
            "created_date_start": created_date_start, "created_date_end": created_date_end,
            "created_precision": created_precision,
            "passages": [{"id": f"lp-{doc_id}", "position": 0, "text": text,
                          "char_start": 0, "char_end": len(text), "node_id": None}],
        }
        self.w.texts[doc_id] = text
        return {"document_id": doc_id, "passage_count": 1}

    async def update_document(self, document_id, *, title=None, document_type=None,
                              created_date_start=None, created_date_end=None,
                              created_precision=None, clear_created_date=False, metadata=None):
        doc = self.w.documents.get(str(document_id))
        if doc is None:
            return None
        if title is not None:
            doc["title"] = title
        if document_type is not None:
            doc["document_type"] = document_type
        if clear_created_date:
            doc.update(created_date_start=None, created_date_end=None, created_precision=None)
        for key, value in (("created_date_start", created_date_start),
                           ("created_date_end", created_date_end),
                           ("created_precision", created_precision)):
            if value is not None:
                doc[key] = value
        doc["metadata"] = {**doc["metadata"], **(metadata or {})}
        return {"document_id": doc["id"], "metadata": doc["metadata"]}

    async def delete_document(self, document_id):
        return self.w.documents.pop(str(document_id), None) is not None

    async def find_existing(self, *, source=None, source_pattern=None):
        return [
            {"document_id": doc["id"], "title": doc["title"], "source": doc["source"],
             "document_type": doc["document_type"], "metadata": doc["metadata"]}
            for doc in self.w.documents.values()
            if source_pattern and source_pattern in doc["source"]
        ]


class Events:
    def __init__(self, world: World) -> None:
        self.w = world

    async def upsert(self, event):
        key = (event["event_type"], event["source_passage_id"])
        existing = next((e for e in self.w.events.values()
                         if (e.event_type, e.source_passage_id) == key), None)
        event_id = existing.id if existing else self.w.new_id()
        self.w.events[event_id] = SimpleNamespace(
            id=event_id,
            event_type=event["event_type"],
            source_passage_id=event["source_passage_id"],
            timestamp_start=datetime.fromisoformat(event["timestamp_start"]),
            payload=event["payload"],
            actors=event.get("actors", []),
            location_text=event.get("location_text"),
            confidence=event["confidence"],
        )
        return self.w.events[event_id]

    async def delete(self, event_id):
        return self.w.events.pop(str(event_id), None) is not None

    async def query(self, filters, k=1000, group_by=None):
        found = [
            e for e in self.w.events.values()
            if (not filters.event_types or e.event_type in filters.event_types)
            and all(e.payload.get(key) == value for key, value in (filters.payload or {}).items())
        ]
        return found[:k], []

    async def get_actors_many(self, event_ids):
        return {str(i): self.w.events[str(i)].actors for i in event_ids if str(i) in self.w.events}


def clients(world: World, readings: dict[str, dict] | None = None):
    return {
        "corpus": Corpus(world),
        "extraction": Extraction(world, READINGS if readings is None else readings),
        "event": Events(world),
        "ingestion": Ingestion(world),
    }


async def run(world, readings=None, **kwargs):
    return await tool_handler(**clients(world, readings), volume_id=VOLUME, **kwargs)


def letters_of(world: World) -> list[dict]:
    return sorted(
        (d for d in world.documents.values() if d["document_type"] == "letter"),
        key=lambda d: d["metadata"]["parent_char_start"],
    )


async def test_a_document_that_is_not_a_collection_is_refused():
    world = World()
    world.add_volume(document_type="generic")
    result = await run(world)
    assert "mode='configure'" in result["error"]


async def test_a_dry_run_decides_and_writes_nothing():
    world = World()
    result = await run(world, chronology_tolerance_days=180)

    assert result["dry_run"] is True
    summary = result["summary"]
    assert summary["openings_in_excluded_ranges"] == 1, "the catalogue's 'letter'"
    assert summary["letters"] == 6
    assert summary["held_by_reason"] == {"chronology_conflict": 1}
    by_start = {row["char_start"]: row for row in result["letters"]}
    geneva = by_start[AT["geneva"]]
    assert geneva["date"]["start"] == "1814-07-01"
    assert geneva["received"]["start"] == "1814-07-18"
    assert by_start[AT["rome1816"]]["hold_reason"] == "chronology_conflict"
    assert letters_of(world) == [] and world.events == {}


async def test_a_letter_ends_where_the_next_begins_and_journals_stay_inside():
    world = World()
    result = await run(world, chronology_tolerance_days=180)
    rome = next(r for r in result["letters"] if r["char_start"] == AT["rome"])
    assert rome["char_end"] <= AT["geneva"]
    assert "Volta" in TEXT[rome["char_start"]:rome["char_end"]], "the journal entry is not a letter"


async def test_applying_without_a_tolerance_is_refused():
    world = World()
    result = await run(world, dry_run=False)
    assert "without a chronology tolerance" in result["error"]
    assert letters_of(world) == []


async def test_apply_writes_dated_letters_and_their_events():
    world = World()
    result = await run(world, dry_run=False, chronology_tolerance_days=180)

    assert "error" not in result
    letters = letters_of(world)
    assert len(letters) == 6
    geneva = next(d for d in letters if d["metadata"]["parent_char_start"] == AT["geneva"])
    assert geneva["title"] == "Faraday to Margaret Faraday, Geneva, 1 July 1814"
    assert geneva["created_date_start"] == "1814-07-01T00:00:00+00:00"
    assert geneva["metadata"]["recipient_entity_id"] == MOTHER
    assert geneva["metadata"]["review_status"] == "auto"
    assert geneva["source"] == f"letter-collection:{VOLUME}#{AT['geneva']}"

    held = next(d for d in letters if d["metadata"]["parent_char_start"] == AT["rome1816"])
    assert held["created_date_start"] is None
    assert held["metadata"]["review_status"] == "needs_review"
    assert held["metadata"]["candidate_date"]["start"] == "1816-02-13"
    assert held["metadata"]["neighbours"]["previous"]["date"]["start"] == "1814-08-06"

    assert len(world.events) == 5, "one per dated letter, none for the held one"
    event = next(e for e in world.events.values() if e.payload["letter_document_id"] == geneva["id"])
    assert {a["role"] for a in event.actors} == {"recipient"}, "the writer is not resolved here"
    assert event.location_text == "Geneva"


async def test_the_edition_s_sender_fills_a_heading_that_names_none():
    world = World()
    await run(world, dry_run=False, chronology_tolerance_days=180)
    abbott = next(d for d in letters_of(world) if d["metadata"]["parent_char_start"] == AT["abbott"])
    assert abbott["metadata"]["sender_entity_id"] == FARADAY
    assert "sender_by_edition_convention" in abbott["metadata"]["flags"]


async def test_a_second_run_changes_nothing():
    world = World()
    await run(world, dry_run=False, chronology_tolerance_days=180)
    documents = {d["id"]: dict(d["metadata"]) for d in letters_of(world)}
    events = {e.id: e.source_passage_id for e in world.events.values()}

    result = await run(world, dry_run=False, chronology_tolerance_days=180)

    assert {d["id"] for d in letters_of(world)} == set(documents)
    assert {e.id: e.source_passage_id for e in world.events.values()} == events
    assert result["superseded"] == []


async def test_a_letter_that_disappears_is_superseded():
    """Its opening is gone, so the letter before it now runs on to Brussels.

    Both old documents go: the vanished letter, and the previous letter's
    shorter self — replaced by a document under the same source with the
    longer text.
    """
    world = World()
    await run(world, dry_run=False, chronology_tolerance_days=180)
    by_start = {d["metadata"]["parent_char_start"]: d for d in letters_of(world)}
    huxtable, before_it = by_start[AT["huxtable"]], by_start[AT["rome1816"]]
    fewer = {k: v for k, v in READINGS.items() if k != "huxtable"}

    result = await run(world, readings=fewer, dry_run=False, chronology_tolerance_days=180)

    superseded = {s["letter_document_id"] for s in result["superseded"]}
    assert superseded == {huxtable["id"], before_it["id"]}
    assert huxtable["id"] not in world.documents
    [longer] = [d for d in letters_of(world) if d["source"] == before_it["source"]]
    assert "Huxtable" in world.texts[longer["id"]]
    assert not [e for e in world.events.values() if e.payload["letter_document_id"] in superseded]


async def test_a_failure_part_way_stops_and_supersedes_nothing():
    world = World()
    await run(world, dry_run=False, chronology_tolerance_days=180)
    before = {d["id"] for d in letters_of(world)}
    world.fail_on_ingest = world.ingests + 2

    result = await run(world, readings={k: v for k, v in READINGS.items() if k != "brussels"},
                       dry_run=False, chronology_tolerance_days=180)

    assert "stopped at letter 2" in result["error"]
    assert before <= {d["id"] for d in letters_of(world)}, "nothing was deleted"


async def test_a_reviewer_s_date_is_applied_and_kept():
    world = World()
    await run(world, dry_run=False, chronology_tolerance_days=180)
    held = next(d for d in letters_of(world) if d["metadata"]["review_status"] == "needs_review")

    accepted = await run(
        world, dry_run=False, chronology_tolerance_days=180,
        accept=[{"letter_document_id": held["id"], "date": "1815-02-13"}],
    )
    assert accepted["summary"]["held"] == 0
    letter = world.documents[held["id"]]
    assert letter["metadata"]["review_status"] == "accepted"
    assert letter["created_date_start"] == "1815-02-13T00:00:00+00:00"
    assert "reviewed" in letter["metadata"]["flags"]

    again = await run(world, dry_run=False, chronology_tolerance_days=180)
    assert again["summary"]["held"] == 0, "the acceptance survives a run without accept"
    assert world.documents[held["id"]]["metadata"]["review_status"] == "accepted"


async def test_accepting_an_unknown_letter_is_reported():
    world = World()
    result = await run(world, chronology_tolerance_days=180,
                       accept=[{"letter_document_id": "nope", "date": "1815-02-13"}])
    assert any("no letter of this volume" in note for note in result["notes"])


async def test_the_review_queue_lists_what_is_held():
    world = World()
    await run(world, dry_run=False, chronology_tolerance_days=180)

    queue = await tool_handler(**clients(world), mode="review_queue")

    assert queue["count"] == 1
    [item] = queue["held"]
    assert item["hold_reason"] == "chronology_conflict"
    assert item["dateline_as_written"] == "' Rome : February 13, 1816."
    assert item["candidate_date"]["start"] == "1816-02-13"


async def test_configure_marks_a_volume_and_checks_its_settings():
    world = World()
    world.add_volume(document_type="generic")
    bad = await tool_handler(**clients(world), volume_id=VOLUME, mode="configure",
                             config={"excluded_ranges": [[9, 3]], "colour": "red"})
    assert bad["error"] == "invalid configuration"
    assert len(bad["problems"]) == 2

    done = await tool_handler(**clients(world), volume_id=VOLUME, mode="configure", dry_run=False,
                              config={"chronology_tolerance_days": 180, "editor": "Henry Bence Jones"})
    assert done["stored"]["metadata"]["chronology_tolerance_days"] == 180
    assert world.documents[VOLUME]["document_type"] == "letter_collection"


@pytest.mark.parametrize("mode", ["structure", "configure"])
async def test_a_volume_is_required(mode):
    world = World()
    result = await tool_handler(**clients(world), mode=mode)
    assert "volume_id is required" in result["error"]
