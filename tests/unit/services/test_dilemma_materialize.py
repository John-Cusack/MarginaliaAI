"""Materializing extraction records into machine claims, without a database.

The materializer talks to six narrow collaborators, so scripted fakes answer
them: one schema, the records under test, one passage the evidence is quoted
from, spans that quote that passage back, and a claims repo that records the
drafts it was asked to store. What is under test is what travels from a
record into its draft — the sentence-group attributes of Decision 005 — and
the bundling tripwire counted alongside it. Storage itself, and the span
verification against canonical text, belong to the integration suite.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from research_engine.domain.extractions import ExtractionRecord, ExtractionSchema
from research_engine.services.dilemma.materialize import (
    ClaimMaterializer,
    bundled_looking,
)

PASSAGE_ID = uuid4()
DOCUMENT_ID = uuid4()
RUN_ID = uuid4()

PASSAGE_TEXT = (
    "The caravan paid no toll at the northern gate. "
    "It carried books and salt, and crossed by daylight."
)

SCHEMA = ExtractionSchema(
    id=uuid4(),
    name="materialize_claims",
    version=1,
    owner="test",
    schema={
        "record_types": [
            {
                "id": "claim",
                "fields": {
                    "statement": {"type": "string", "required": True},
                    "evidence": {"type": "evidence_span", "required": True},
                },
            },
            {
                "id": "vouch",
                "fields": {
                    "statement": {"type": "string", "required": True},
                    "evidence": {"type": "evidence_span", "required": True},
                },
            },
        ]
    },
    prompt_template="Passage: {{ passage_text }}",
    created_at=datetime.now(UTC),
)


class _Extractions:
    def __init__(self, records):
        self._records = records

    async def records_for_schema(self, schema_id, extractor_version=None):
        return list(self._records)


class _Schemas:
    async def get_by_name_version(self, name, version):
        return SCHEMA if name == SCHEMA.name else None


class _Passages:
    async def get(self, passage_id):
        if passage_id != PASSAGE_ID:
            return None
        return SimpleNamespace(
            text=PASSAGE_TEXT, char_start=0, document_id=DOCUMENT_ID
        )


class _Spans:
    async def resolve(self, tx, *, document_id, char_start, char_end):
        return SimpleNamespace(
            id=uuid4(), quoted_text=PASSAGE_TEXT[char_start:char_end]
        )


class _Claims:
    """Records every draft; a repeated (record, statement) stores nothing."""

    def __init__(self):
        self.drafts = []
        self._seen = set()

    async def insert_claim(self, tx, draft):
        key = (draft.extraction_record_id, draft.statement)
        if key in self._seen:
            return None
        self._seen.add(key)
        self.drafts.append(draft)
        return SimpleNamespace(id=uuid4())

    async def insert_evidence(self, tx, evidence):
        pass

    async def insert_vouch(self, tx, vouch):
        pass


class _Entities:
    async def search_by_name(self, label, entity_type, limit, exact=False):
        return []


@asynccontextmanager
async def _transaction():
    yield object()


def _claim_data(**overrides):
    data = {
        "statement": "The caravan paid no toll.",
        "polarity": "affirms",
        "modality": "asserts",
        "speaker": "the narrator",
        "evidence": "paid no toll at the northern gate",
        "confidence": 0.9,
    }
    data.update(overrides)
    return data


def _record(data, record_type="claim"):
    return ExtractionRecord(
        id=uuid4(),
        extraction_id=uuid4(),
        passage_id=PASSAGE_ID,
        schema_id=SCHEMA.id,
        record_type=record_type,
        data=data,
        created_at=datetime.now(UTC),
    )


async def _materialize(records):
    claims = _Claims()
    materializer = ClaimMaterializer(
        _Extractions(records),
        _Schemas(),
        _Passages(),
        _Spans(),
        claims,
        _Entities(),
        _transaction,
    )
    stats = await materializer.materialize(
        run_id=RUN_ID, schema_ref="materialize_claims:1"
    )
    return stats, claims


def test_semicolon_in_the_statement_looks_bundled() -> None:
    assert bundled_looking(
        "The caravan carried books; it carried salt.", "carried books and salt"
    )


def test_semicolon_evidence_joined_by_and_looks_bundled() -> None:
    assert bundled_looking(
        "The caravan carried books and salt.",
        "carried books; it also carried salt",
    )


def test_two_comma_ands_look_bundled() -> None:
    assert bundled_looking(
        "The caravan was large, and well guarded, and crossed by daylight.",
        "large and well guarded",
    )


def test_a_single_simple_statement_does_not_look_bundled() -> None:
    assert not bundled_looking(
        "The caravan paid no toll.", "paid no toll at the northern gate"
    )


def test_semicolon_evidence_alone_does_not_look_bundled() -> None:
    assert not bundled_looking(
        "The caravan paid no toll.",
        "paid no toll at the northern gate; the gate stood open",
    )


async def test_group_fields_travel_into_the_claim_attributes() -> None:
    record = _record(_claim_data(group=2, group_relation="composite_act"))

    stats, claims = await _materialize([record])

    assert stats["claims_created"] == 1
    assert claims.drafts[0].attributes == {
        "group": 2,
        "group_relation": "composite_act",
    }


async def test_a_float_group_with_an_integral_value_is_stored_as_an_int() -> None:
    record = _record(_claim_data(group=3.0))

    _, claims = await _materialize([record])

    assert claims.drafts[0].attributes == {"group": 3}


async def test_malformed_group_fields_are_omitted_and_the_claim_is_kept() -> None:
    record = _record(_claim_data(group=True, group_relation="nonsense"))

    stats, claims = await _materialize([record])

    assert stats["claims_created"] == 1
    assert claims.drafts[0].attributes == {}


async def test_bundled_flagged_counts_created_bundled_claims_only() -> None:
    clean = _record(_claim_data())
    bundled = _record(
        _claim_data(
            statement="The caravan carried books; it carried salt.",
            evidence="carried books and salt",
        )
    )

    stats, _ = await _materialize([clean, bundled])

    assert stats["claims_created"] == 2
    assert stats["bundled_flagged"] == 1
