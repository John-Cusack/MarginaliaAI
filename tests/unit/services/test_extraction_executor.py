"""What the executor keeps.

The engine's first principle is that an LLM's conclusions land in a table with
their source passage, so they can be inspected and cited. That makes persistence
the behaviour worth testing: the executor returned records to its caller and
wrote nothing, so `extractions` and `extraction_records` were empty by
construction, and every re-run paid full price for an answer already given.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research_engine.domain.common import ExtractionStatus
from research_engine.domain.errors import LLMError
from research_engine.domain.extractions import ExtractionOptions, ExtractionSchema
from research_engine.services.extraction.executor import ExtractionExecutor
from research_engine.testing.corpus import new_id

PASSAGE_TEXT = (
    "Paul's use of dikaiosyne here is forensic rather than transformative, "
    "as Bultmann argued at length."
)

SCHEMA_DEF = {
    "id": "interpretive_claims",
    "version": 1,
    "record_types": [
        {
            "id": "claim",
            "fields": {
                "assertion": {"type": "string", "required": True},
                "quote": {"type": "evidence_span", "required": True},
                "confidence": {"type": "number", "range": [0, 1]},
            },
        }
    ],
    "prompt": "Extract claims from: {{ passage_text }}",
}


def a_schema() -> ExtractionSchema:
    return ExtractionSchema(
        id=uuid4(),
        name="interpretive_claims",
        version=1,
        owner="core",
        schema=SCHEMA_DEF,
        prompt_template=SCHEMA_DEF["prompt"],
        created_at=datetime.now(UTC),
    )


def a_good_answer(quote: str = "forensic rather than transformative"):
    return {
        "records": [
            {
                "record_type": "claim",
                "fields": {
                    "assertion": "Paul's dikaiosyne is forensic",
                    "quote": quote,
                    "confidence": 0.8,
                },
            }
        ]
    }


class FakeLLM:
    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls: list[dict] = []

    async def structured(self, messages, schema, model=None, caller="core", purpose=""):
        self.calls.append({"purpose": purpose, "prompt": messages[0]["content"]})
        answer = self._answers.pop(0) if self._answers else {"records": []}
        if isinstance(answer, Exception):
            raise answer
        return answer, uuid4()

    async def complete(self, *a, **kw):  # pragma: no cover - unused here
        raise NotImplementedError


class FakePassages:
    def __init__(self, passage):
        self._passage = passage

    async def get(self, passage_id):
        return self._passage if passage_id == self._passage.id else None


class FakeExtractions:
    """Records what was written, keyed the way the unique constraint keys it."""

    def __init__(self):
        self.saved: dict[tuple, object] = {}
        self.records: dict[object, list] = {}
        self.lookups: list[tuple] = []

    async def get_by_key(self, passage_id, schema_id, extractor_version):
        self.lookups.append((passage_id, schema_id, extractor_version))
        return self.saved.get((passage_id, schema_id, extractor_version))

    async def save(self, tx, extraction):
        key = (
            extraction.passage_id,
            extraction.schema_id,
            extraction.extractor_version,
        )
        self.saved[key] = extraction
        return extraction

    async def replace_records(self, tx, extraction_id, records):
        self.records[extraction_id] = list(records)


class FakeSchemas:
    def __init__(self, schema):
        self._schema = schema

    async def get_by_name_version(self, name, version):
        if name == self._schema.name and version == self._schema.version:
            return self._schema
        return None


@asynccontextmanager
async def fake_transaction():
    yield SimpleNamespace(conn=None)


def build(llm, extractions=None, schema=None, passage=None):
    schema = schema or a_schema()
    passage = passage or SimpleNamespace(id=new_id(), text=PASSAGE_TEXT)
    extractions = extractions if extractions is not None else FakeExtractions()
    executor = ExtractionExecutor(
        llm=llm,
        passages=FakePassages(passage),
        extractions=extractions,
        extraction_schemas=FakeSchemas(schema),
        transaction_factory=fake_transaction,
        default_model="claude-sonnet-4-5-20250929",
    )
    return executor, extractions, passage, schema


class TestPersistence:
    @pytest.mark.asyncio
    async def test_a_successful_run_is_stored(self):
        executor, extractions, passage, schema = build(FakeLLM(a_good_answer()))

        batch = await executor.execute([passage.id], "interpretive_claims:1")

        assert batch.results[0].status == ExtractionStatus.ok
        [stored] = list(extractions.saved.values())
        assert stored.passage_id == passage.id
        assert stored.schema_id == schema.id
        assert stored.status == ExtractionStatus.ok
        assert stored.llm_model == "claude-sonnet-4-5-20250929"

    @pytest.mark.asyncio
    async def test_records_are_materialized_with_offsets_into_the_passage(self):
        executor, extractions, passage, _ = build(FakeLLM(a_good_answer()))

        await executor.execute([passage.id], "interpretive_claims:1")

        [record] = next(iter(extractions.records.values()))
        assert record.record_type == "claim"
        assert record.passage_id == passage.id
        assert PASSAGE_TEXT[record.evidence_start : record.evidence_end] == (
            "forensic rather than transformative"
        )

    @pytest.mark.asyncio
    async def test_a_failure_is_stored_too(self):
        """A run that failed is a finding, not an absence."""
        executor, extractions, passage, _ = build(FakeLLM(LLMError("provider down")))

        batch = await executor.execute([passage.id], "interpretive_claims:1")

        assert batch.results[0].status == ExtractionStatus.failed
        [stored] = list(extractions.saved.values())
        assert stored.status == ExtractionStatus.failed
        assert "provider down" in stored.error
        assert extractions.records[stored.id] == []

    @pytest.mark.asyncio
    async def test_invented_evidence_fails_the_record(self):
        llm = FakeLLM(
            a_good_answer("a sentence that is not in the passage"),
            a_good_answer("a sentence that is not in the passage"),
        )
        executor, extractions, passage, _ = build(llm)

        batch = await executor.execute([passage.id], "interpretive_claims:1")

        assert batch.results[0].status == ExtractionStatus.failed
        assert list(extractions.saved.values())[0].status == ExtractionStatus.failed


class TestCaching:
    @pytest.mark.asyncio
    async def test_the_cache_is_keyed_by_what_it_is_stored_under(self):
        """The lookup used to pass the model where extractor_version belongs.

        `extractions` is unique on (passage, schema, extractor_version), so a
        lookup keyed on the model matched nothing that a write could ever
        produce — the cache could not hit even once it had rows to hit.
        """
        executor, extractions, passage, _ = build(FakeLLM(a_good_answer()))

        await executor.execute([passage.id], "interpretive_claims:1")

        [(_, _, looked_up)] = extractions.lookups
        assert looked_up in {key[2] for key in extractions.saved}

    @pytest.mark.asyncio
    async def test_a_second_run_does_not_call_the_model_again(self):
        llm = FakeLLM(a_good_answer())
        executor, extractions, passage, _ = build(llm)

        await executor.execute([passage.id], "interpretive_claims:1")
        second = await executor.execute([passage.id], "interpretive_claims:1")

        assert len(llm.calls) == 1
        assert second.results[0].from_cache is True

    @pytest.mark.asyncio
    async def test_a_cached_failure_is_retried_rather_than_served(self):
        """Otherwise a provider outage becomes permanent for every passage."""
        llm = FakeLLM(LLMError("transient"), a_good_answer())
        executor, extractions, passage, _ = build(llm)

        first = await executor.execute([passage.id], "interpretive_claims:1")
        second = await executor.execute([passage.id], "interpretive_claims:1")

        assert first.results[0].status == ExtractionStatus.failed
        assert second.results[0].status == ExtractionStatus.ok
        assert second.results[0].from_cache is False

    @pytest.mark.asyncio
    async def test_editing_the_prompt_invalidates_the_cache(self):
        """A prompt edit changes the answer, so it must change the key.

        Keying on the schema's version number alone would serve records written
        by the old prompt for as long as the version stayed put — which, while a
        schema is being developed, is the entire time it matters.
        """
        schema = a_schema()
        llm = FakeLLM(a_good_answer(), a_good_answer())
        executor, extractions, passage, _ = build(llm, schema=schema)
        await executor.execute([passage.id], "interpretive_claims:1")

        schema.prompt_template = "Find every claim in: {{ passage_text }}"
        result = await executor.execute([passage.id], "interpretive_claims:1")

        assert len(llm.calls) == 2
        assert result.results[0].from_cache is False
        assert len(extractions.saved) == 2


class TestRetry:
    @pytest.mark.asyncio
    async def test_a_misquote_is_given_the_error_and_one_more_go(self):
        llm = FakeLLM(a_good_answer("not in the passage at all"), a_good_answer())
        executor, extractions, passage, _ = build(llm)

        batch = await executor.execute([passage.id], "interpretive_claims:1")

        assert batch.results[0].status == ExtractionStatus.ok
        assert llm.calls[1]["purpose"] == "extraction_retry"
        assert "rejected" in llm.calls[1]["prompt"]

    @pytest.mark.asyncio
    async def test_retry_can_be_turned_off(self):
        llm = FakeLLM(a_good_answer("not in the passage at all"))
        executor, _, passage, _ = build(llm)

        batch = await executor.execute(
            [passage.id],
            "interpretive_claims:1",
            ExtractionOptions(retry_on_validation_error=False),
        )

        assert batch.results[0].status == ExtractionStatus.failed
        assert len(llm.calls) == 1


class TestSchemaResolution:
    @pytest.mark.asyncio
    async def test_an_unknown_schema_is_named_in_the_error(self):
        executor, _, passage, _ = build(FakeLLM())
        with pytest.raises(ValueError, match="missing:1"):
            await executor.execute([passage.id], "missing:1")

    @pytest.mark.asyncio
    async def test_a_missing_passage_writes_nothing(self):
        """`extractions.passage_id` is a foreign key; there is no row to write."""
        executor, extractions, _, _ = build(FakeLLM())

        batch = await executor.execute([new_id()], "interpretive_claims:1")

        assert batch.results[0].status == ExtractionStatus.failed
        assert extractions.saved == {}
