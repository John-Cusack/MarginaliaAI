"""Placing openings on the volume, cutting it into letters, and naming actors."""

from __future__ import annotations

from pathlib import Path

from history.letters import openings, units
from history.letters.dating import Reading, decide
from letter_fixtures import opening_data

from research_engine.services.extraction.registration import (
    draft_from_yaml,
    validate_schema_definition,
)

TEXT = (
    "FARADAY TO HIS MOTHER.\n\n' Geneva : July 1. Received July 18.\n\n"
    "' I hope, dear Mother, that you are in good health.\n\n"
    "TO ME. E. G. ABBOTT.\n\n' Geueva : Saturday, August 6, 1814.\n\n' Dear Eobert...\n\n"
    "CHAPTEE IV.\n\nNarrative that belongs to no letter.\n"
)
ABBOTT_AT = TEXT.index("TO ME. E. G. ABBOTT.")
CHAPTER_AT = TEXT.index("CHAPTEE IV.")


def record(rid, passage_id, evidence_start, data):
    return {"id": rid, "passage_id": passage_id, "evidence_start": evidence_start, "data": data}


PASSAGES = {
    # Two overlapping passages: the second starts mid-way through the first
    # letter and holds the whole second one, so its heading is read twice.
    "p1": {"id": "p1", "char_start": 0, "char_end": ABBOTT_AT + 30},
    "p2": {"id": "p2", "char_start": 50, "char_end": len(TEXT)},
}
GENEVA = opening_data("FARADAY TO HIS MOTHER.", dateline="' Geneva : July 1. Received July 18.")
ABBOTT = opening_data("TO ME. E. G. ABBOTT.", dateline="' Geueva : Saturday, August 6, 1814.")


class TestPlacement:
    def test_overlap_reads_one_heading_twice_and_it_is_merged(self):
        placed = openings.place(
            [
                record("r1", "p1", 0, GENEVA),
                record("r2", "p1", ABBOTT_AT, ABBOTT),
                record("r3", "p2", ABBOTT_AT - 50, {**ABBOTT, "confidence": 0.95}),
            ],
            PASSAGES,
            TEXT,
        )
        assert [o.start for o in placed.openings] == [0, ABBOTT_AT]
        assert placed.merged == 1
        merged = placed.openings[1]
        assert merged.record_ids == ["r2", "r3"]
        assert merged.confidence == 0.95, "the more confident reading is kept"

    def test_readings_a_few_characters_apart_are_one(self):
        placed = openings.place(
            [record("r2", "p1", ABBOTT_AT, ABBOTT), record("r3", "p1", ABBOTT_AT, ABBOTT)],
            PASSAGES,
            TEXT,
        )
        assert len(placed.openings) == 1

    def test_a_quotation_not_at_its_offset_is_reported_not_placed(self):
        placed = openings.place([record("r1", "p1", 7, GENEVA)], PASSAGES, TEXT)
        assert placed.openings == []
        assert placed.misplaced[0]["record_id"] == "r1"

    def test_an_opening_in_an_excluded_range_is_dropped(self):
        placed = openings.place(
            [record("r1", "p1", 0, GENEVA), record("r2", "p1", ABBOTT_AT, ABBOTT)],
            PASSAGES,
            TEXT,
            excluded_ranges=[(ABBOTT_AT, len(TEXT))],
        )
        assert [o.start for o in placed.openings] == [0]
        assert placed.excluded == 1


class TestSlicing:
    def placed(self):
        return openings.place(
            [record("r1", "p1", 0, GENEVA), record("r2", "p1", ABBOTT_AT, ABBOTT)],
            PASSAGES,
            TEXT,
        ).openings

    def test_a_letter_runs_to_the_next_opening(self):
        outline = [{"title": "CHAPTEE IV.", "char_start": CHAPTER_AT, "depth": 1}]
        spans = units.spans(self.placed(), TEXT, units.cut_points(outline, []))
        assert spans[0] == (0, TEXT.rindex("health.") + len("health."))

    def test_a_chapter_heading_ends_the_letter_before_it(self):
        outline = [{"title": "CHAPTEE IV.", "char_start": CHAPTER_AT, "depth": 1}]
        spans = units.spans(self.placed(), TEXT, units.cut_points(outline, []))
        assert TEXT[spans[1][0] : spans[1][1]].endswith("Dear Eobert...")
        assert "Narrative" not in TEXT[spans[1][0] : spans[1][1]]

    def test_ocr_chapter_spellings_are_recognised(self):
        outline = [
            {"title": title, "char_start": i, "depth": 1}
            for i, title in enumerate(["CHAPTEE I.", "CIIAPTEll I.", "CHAPTER ni.", "FARADAY TO ABBOTT."])
        ]
        assert units.cut_points(outline, []) == [0, 1, 2]

    def test_an_excluded_range_also_cuts(self):
        assert units.cut_points([], [(500, 900)]) == [500]


class TestActors:
    def opening(self, **data):
        return openings.Opening(start=0, record_ids=["r"], passage_id="p", data=data)

    def test_core_s_resolution_wins(self):
        sender, _, flags = units.resolve_actors(
            self.opening(
                sender="Faraday",
                sender_resolved={"entity_id": "f-1", "canonical_name": "Michael Faraday"},
                recipient="Abbott",
                recipient_resolved={"entity_id": "a-1", "canonical_name": "Benjamin Abbott"},
            )
        )
        assert (sender["entity_id"], sender["how"]) == ("f-1", "resolved")
        assert flags == []

    def test_kinship_words_come_from_the_volume_s_map(self):
        _, recipient, _ = units.resolve_actors(
            self.opening(recipient="his mother"),
            alias_map={"His Mother": {"entity_id": "m-1", "name": "Margaret Faraday"}},
        )
        assert (recipient["entity_id"], recipient["name"], recipient["how"]) == (
            "m-1",
            "Margaret Faraday",
            "alias_map",
        )

    def test_a_heading_naming_only_the_recipient_takes_the_edition_s_sender(self):
        sender, _, flags = units.resolve_actors(
            self.opening(recipient="Mrs. Faraday"),
            default_sender={"entity_id": "f-1", "name": "Michael Faraday"},
        )
        assert (sender["entity_id"], sender["how"]) == ("f-1", "default_sender")
        assert "sender_by_edition_convention" in flags

    def test_a_written_name_that_will_not_resolve_is_never_defaulted(self):
        """"WOLLASTON" unresolved is not Faraday — it may be anyone."""
        sender, _, flags = units.resolve_actors(
            self.opening(sender="Wollaston"), default_sender="f-1"
        )
        assert sender["entity_id"] is None
        assert "actor_unresolved" in flags

    def test_the_event_carries_direction_in_its_actors(self):
        [decision] = decide(
            [Reading.of(opening_data("FARADAY TO ABBOTT.", dateline="' Rome : January 25, 1815.", date_written="January 25, 1815", place="Rome"))],
            tolerance_days=None,
        )
        unit = units.Unit(
            0,
            self.opening(place="Rome"),
            0,
            10,
            decision,
            sender={"surface": "FARADAY", "entity_id": "f-1", "name": "Michael Faraday"},
            recipient={"surface": "ABBOTT", "entity_id": None, "name": None},
        )
        event = units.event_for(
            unit, letter_document_id="l-1", source_passage_id="p-1", volume_id="v-1"
        )
        assert event["actors"] == [{"entity_id": "f-1", "role": "sender"}]
        assert {a["role"] for a in event["actors"]} <= units.ROLES[units.EVENT_TYPE]
        assert event["timestamp_start"] == "1815-01-25T00:00:00+00:00"
        assert event["location_text"] == "Rome"
        assert event["payload"]["letter_document_id"] == "l-1"


class TestDescription:
    def test_a_letter_document_says_where_it_came_from(self):
        [decision] = decide(
            [Reading.of(opening_data("TO MRS. EAEADAT.", dateline="' Marino : Sunday, July 28, 1822.", date_written="July 28, 1822", weekday="sunday", place="Marino"))],
            tolerance_days=None,
        )
        unit = units.Unit(
            4,
            openings.Opening(start=537451, record_ids=["r"], passage_id="p", data={"place": "Marino", "opening": "TO MRS. EAEADAT."}),
            537451,
            544346,
            decision,
            sender={"surface": None, "entity_id": "f-1", "name": "Michael Faraday"},
            recipient={"surface": "Mrs. Faraday", "entity_id": None, "name": None},
        )
        meta = units.letter_metadata(unit, volume_id="vol-1", unit_count=90)
        assert (meta["parent_document_id"], meta["parent_char_start"], meta["parent_char_end"]) == (
            "vol-1",
            537451,
            544346,
        )
        assert meta["author"] == "Michael Faraday"
        assert meta["review_status"] == "auto"
        assert units.title_for(unit) == "Michael Faraday to Mrs. Faraday, Marino, 28 July 1822"
        assert units.document_dates(decision) == {
            "created_date_start": "1822-07-28T00:00:00+00:00",
            "created_date_end": "1822-07-28T23:59:59+00:00",
            "created_precision": "day",
        }

    def test_a_source_is_its_volume_and_offset(self):
        assert units.source_for("vol-1", 218864) == "letter-collection:vol-1#218864"


def test_the_schema_registers():
    yaml = (
        Path(__file__).parents[1] / "history/schemas/extraction_schemas/letter_openings.yaml"
    ).read_text()
    draft = draft_from_yaml(yaml)
    validate_schema_definition(draft.schema_def, draft.prompt_template)
    fields = draft.schema_def["record_types"][0]["fields"]
    assert list(fields)[0] == "opening", "the first evidence field is the anchor"
    assert fields["dateline"]["scan"] == "dates"
    assert fields["received_date"]["resolve"] == "forward"


def test_two_readings_of_one_opening_that_disagree_are_merged_and_held():
    """Two letters do not begin a few characters apart; two readings of one do."""
    july = opening_data("FARADAY TO HIS MOTHER.", dateline="' Geneva : July 1.", date_written="July 1")
    june = opening_data("FARADAY TO HIS MOTHER.", dateline="' Geneva : June 1.", date_written="June 1")
    placed = openings.place(
        [record("r1", "p1", 0, july), record("r2", "p1", 0, june)], PASSAGES, TEXT
    )
    assert len(placed.openings) == 1
    [decision] = decide([Reading.of(placed.openings[0].data)], tolerance_days=None)
    assert decision.hold == "reader_disagreement"
