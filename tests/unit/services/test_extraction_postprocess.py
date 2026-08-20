"""Resolving the field types that are not really strings.

Both of these are declared by the history pack's `epistolary_references`, which
is the schema for reading Civil War correspondence, and both were stored as
whatever text the model produced. `find_missing_letters` then compared
"the 15th ult." against a timeline of datetimes and an invented UUID against the
entity store, matched nothing in either case, and reported no missing letters —
which reads exactly like there being none.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research_engine.services.extraction.postprocess import RecordEnricher
from research_engine.services.extraction.validation import ValidatedRecord
from research_engine.testing.corpus import new_id

DOCUMENT_ID = new_id()
ANCHOR = datetime(1862, 5, 20, tzinfo=UTC)

#: The real field shapes from `epistolary_references`.
RECORD_TYPES = {
    "epistolary_reference": {
        "id": "epistolary_reference",
        "fields": {
            "referenced_party_surface": {"type": "string"},
            "referenced_party_entity_id": {"type": "entity_ref", "entity_type": "person"},
            "referenced_date": {"type": "fuzzy_date"},
            "evidence": {"type": "evidence_span"},
        },
    }
}


class FakeDocuments:
    def __init__(self, created_date_start=ANCHOR):
        self._date = created_date_start
        self.calls = 0

    async def get(self, document_id):
        self.calls += 1
        return SimpleNamespace(id=document_id, created_date_start=self._date)


class FakeEntities:
    def __init__(self, *candidates):
        self._candidates = list(candidates)

    async def search_by_name(self, name, entity_type=None, k=5):
        return self._candidates[:k]


def candidate(name, score, entity_type="person"):
    return SimpleNamespace(
        entity_id=uuid4(),
        canonical_name=name,
        entity_type=entity_type,
        match_score=score,
    )


def a_record(**fields):
    return ValidatedRecord(
        record_type="epistolary_reference",
        fields={"evidence": "yours of the 3d ult.", **fields},
        evidence_start=0,
        evidence_end=20,
    )


def a_passage(node_id=None):
    return SimpleNamespace(
        id=new_id(), document_id=DOCUMENT_ID, text="x", node_id=node_id
    )


class FakeNodes:
    """Structure nodes, one letter apiece, some dated."""

    def __init__(self, dates: dict, ancestors: dict | None = None):
        self._dates = dates
        self._ancestors = ancestors or {}

    async def get(self, node_id):
        if node_id not in self._dates and node_id not in self._ancestors:
            return None
        date = self._dates.get(node_id)
        return SimpleNamespace(
            id=node_id,
            depth=2,
            metadata={"date_start": date} if date else {},
        )

    async def get_ancestors(self, node_id):
        return [
            SimpleNamespace(id=nid, depth=depth, metadata={"date_start": date})
            for nid, depth, date in self._ancestors.get(node_id, [])
        ]


async def enrich(records, documents=None, entities=None, nodes=None, passage=None):
    enricher = RecordEnricher(
        documents=documents or FakeDocuments(),
        entities=entities or FakeEntities(),
        document_nodes=nodes,
    )
    return await enricher.enrich(records, passage or a_passage(), RECORD_TYPES)


pytestmark = pytest.mark.asyncio


class TestFuzzyDate:
    async def test_an_epistolary_date_becomes_a_span(self):
        [result] = await enrich([a_record(referenced_date="the 3d ult.")])

        resolved = result.fields["referenced_date_resolved"]
        assert resolved["start"].startswith("1862-04-03")
        assert resolved["precision"] == "day"

    async def test_the_model_s_own_words_are_kept(self):
        """Resolution is written beside the answer, never over it."""
        [result] = await enrich([a_record(referenced_date="yours of the 3d ult.")])

        assert result.fields["referenced_date"] == "yours of the 3d ult."

    async def test_a_date_that_cannot_be_read_resolves_to_null(self):
        """Explicitly null, not absent — the field was tried and did not yield."""
        [result] = await enrich([a_record(referenced_date="your last")])

        assert result.fields["referenced_date_resolved"] is None

    async def test_a_relative_date_needs_the_document_s_own_date(self):
        """"The 3d ult." is meaningless without knowing when the letter was written."""
        [result] = await enrich(
            [a_record(referenced_date="the 3d ult.")],
            documents=FakeDocuments(created_date_start=None),
        )

        assert result.fields["referenced_date_resolved"] is None

    async def test_an_absolute_date_does_not_need_one(self):
        [result] = await enrich(
            [a_record(referenced_date="May 15, 1862")],
            documents=FakeDocuments(created_date_start=None),
        )

        assert result.fields["referenced_date_resolved"]["start"].startswith("1862-05-15")

    async def test_the_document_is_looked_up_once_for_many_records(self):
        """A run over one letterbook asks the same question thousands of times."""
        documents = FakeDocuments()
        enricher = RecordEnricher(documents=documents, entities=FakeEntities())
        passage = a_passage()

        for _ in range(5):
            await enricher.enrich(
                [a_record(referenced_date="the 3d ult.")], passage, RECORD_TYPES
            )

        assert documents.calls == 1


class TestEntityRef:
    async def test_a_name_resolves_to_an_entity(self):
        entities = FakeEntities(candidate("George B. McClellan", 0.97))
        [result] = await enrich(
            [a_record(referenced_party_entity_id="McClellan")], entities=entities
        )

        resolved = result.fields["referenced_party_entity_id_resolved"]
        assert resolved["canonical_name"] == "George B. McClellan"

    async def test_a_weak_match_is_refused(self):
        """Trigram similarity on short surnames gets close enough to be dangerous."""
        entities = FakeEntities(candidate("Burns", 0.62))
        [result] = await enrich(
            [a_record(referenced_party_entity_id="Burnside")], entities=entities
        )

        assert result.fields["referenced_party_entity_id_resolved"] is None

    async def test_two_equally_good_matches_are_refused(self):
        """A tie is a question, not a resolution.

        Silently taking the first would reassign a letter to the wrong man, and
        nothing downstream could tell that from a real attribution.
        """
        entities = FakeEntities(
            candidate("William F. Smith", 0.9), candidate("Charles F. Smith", 0.9)
        )
        [result] = await enrich(
            [a_record(referenced_party_entity_id="Smith")], entities=entities
        )

        assert result.fields["referenced_party_entity_id_resolved"] is None

    async def test_no_candidates_resolves_to_null(self):
        [result] = await enrich([a_record(referenced_party_entity_id="Nobody")])

        assert result.fields["referenced_party_entity_id_resolved"] is None


class TestUntouchedFields:
    async def test_plain_strings_are_left_alone(self):
        [result] = await enrich([a_record(referenced_party_surface="Genl. Burnside")])

        assert result.fields["referenced_party_surface"] == "Genl. Burnside"
        assert "referenced_party_surface_resolved" not in result.fields

    async def test_evidence_is_left_alone(self):
        [result] = await enrich([a_record()])

        assert "evidence_resolved" not in result.fields

    async def test_a_record_with_nothing_to_resolve_is_returned_unchanged(self):
        record = a_record()
        [result] = await enrich([record])

        assert result is record

    async def test_no_records(self):
        assert await enrich([]) == []


class TestAnchoringToTheContainingLetter:
    """Where the date of a bound volume's letters actually lives.

    `civilwarpapersof0000mccl` is one document holding 728 letters. Its own
    `created_date_start` is null and always would be — a collected edition has
    no single date. Each letter's dateline is on its structure node, and that is
    what a relative date inside it is relative to.
    """

    async def test_the_letter_s_date_is_used(self):
        node = new_id()
        [result] = await enrich(
            [a_record(referenced_date="the 3d ult.")],
            documents=FakeDocuments(created_date_start=None),
            nodes=FakeNodes({node: "1862-05-20T00:00:00+00:00"}),
            passage=a_passage(node_id=node),
        )

        assert result.fields["referenced_date_resolved"]["start"].startswith("1862-04-03")

    async def test_the_letter_wins_over_the_volume(self):
        """A volume date, where one exists, is far too coarse to anchor with."""
        node = new_id()
        [result] = await enrich(
            [a_record(referenced_date="the 3d ult.")],
            documents=FakeDocuments(created_date_start=ANCHOR),
            nodes=FakeNodes({node: "1863-01-10T00:00:00+00:00"}),
            passage=a_passage(node_id=node),
        )

        assert result.fields["referenced_date_resolved"]["start"].startswith("1862-12-03")

    async def test_an_undated_node_falls_back_to_the_document(self):
        node = new_id()
        [result] = await enrich(
            [a_record(referenced_date="the 3d ult.")],
            nodes=FakeNodes({node: None}),
            passage=a_passage(node_id=node),
        )

        assert result.fields["referenced_date_resolved"]["start"].startswith("1862-04-03")

    async def test_the_nearest_dated_ancestor_is_used(self):
        """A passage inside a subsection of a letter is still inside the letter."""
        child, letter, volume = new_id(), new_id(), new_id()
        [result] = await enrich(
            [a_record(referenced_date="the 3d ult.")],
            documents=FakeDocuments(created_date_start=None),
            nodes=FakeNodes(
                {child: None},
                ancestors={
                    child: [
                        (volume, 0, "1860-01-01T00:00:00+00:00"),
                        (letter, 2, "1862-05-20T00:00:00+00:00"),
                    ]
                },
            ),
            passage=a_passage(node_id=child),
        )

        assert result.fields["referenced_date_resolved"]["start"].startswith("1862-04-03")

    async def test_without_a_node_repo_the_document_still_anchors(self):
        [result] = await enrich(
            [a_record(referenced_date="the 3d ult.")],
            passage=a_passage(node_id=new_id()),
        )

        assert result.fields["referenced_date_resolved"]["start"].startswith("1862-04-03")
