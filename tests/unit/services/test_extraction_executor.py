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
from research_engine.domain.errors import LLMError, LLMUnavailable
from research_engine.domain.extractions import ExtractionOptions, ExtractionSchema
from research_engine.services.extraction.caching import extractor_version
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
    def __init__(self, passage, before=(), after=()):
        self._passage = passage
        self._before = list(before)
        self._after = list(after)

    async def get(self, passage_id):
        return self._passage if passage_id == self._passage.id else None

    async def get_context(self, passage_id, before=0, after=0):
        return (
            self._before[-before:] if before else [],
            self._passage,
            self._after[:after] if after else [],
        )


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


def build(llm, extractions=None, schema=None, passage=None, before=(), after=()):
    schema = schema or a_schema()
    passage = passage or SimpleNamespace(id=new_id(), text=PASSAGE_TEXT)
    extractions = extractions if extractions is not None else FakeExtractions()
    executor = ExtractionExecutor(
        llm=llm,
        passages=FakePassages(passage, before=before, after=after),
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


class TestAnUnavailableBackend:
    """A missing API key is not eight hundred failed passages."""

    @pytest.mark.asyncio
    async def test_the_run_stops_instead_of_failing_every_passage(self):
        """Found by trying to run the real thing.

        The Anthropic client raises a bare `TypeError` before any request when
        it can resolve no credentials, so nothing recognised it as an LLM
        problem: it escaped every handler in the adapter and killed the batch
        with a traceback. Translated and caught, the run stops at the first
        passage and says what is wrong.
        """
        llm = FakeLLM(LLMUnavailable("No Anthropic credentials are configured."))
        executor, extractions, passage, _ = build(llm)

        with pytest.raises(LLMUnavailable, match="credentials"):
            await executor.execute([passage.id], "interpretive_claims:1")

        # Nothing recorded: no passage failed, the run never happened.
        assert extractions.saved == {}

    @pytest.mark.asyncio
    async def test_an_ordinary_provider_error_still_fails_just_that_passage(self):
        """The distinction this rests on — one bad passage is not a dead backend."""
        llm = FakeLLM(LLMError("content filter"))
        executor, extractions, passage, _ = build(llm)

        batch = await executor.execute([passage.id], "interpretive_claims:1")

        assert batch.results[0].status == ExtractionStatus.failed
        assert len(extractions.saved) == 1


class TestTheCacheKey:
    """Everything that can change the answer must change the key, and vice versa.

    `extractions` is unique on (passage, schema, extractor_version), so a key
    that omits an input serves records written by a different question — and
    an unstable key (say, one sensitive to dict ordering) pays for the same
    extraction over and over.
    """

    BASE = {
        "schema_version": 1,
        "prompt_template": "Extract: {{ passage_text }}",
        "llm_model": "model-a",
        "schema_def": {"record_types": [{"id": "claim", "fields": {"q": {"type": "string"}}}]},
        "context_before": 1,
        "context_after": 0,
        "entity_hints": "Paul",
    }

    def key(self, **overrides):
        return extractor_version(**{**self.BASE, **overrides})

    def test_the_same_inputs_give_the_same_key(self):
        assert self.key() == self.key()

    def test_schema_def_key_order_does_not_change_the_key(self):
        reordered = {"record_types": [{"fields": {"q": {"type": "string"}}, "id": "claim"}]}
        assert self.key(schema_def=reordered) == self.key()

    def test_editing_the_prompt_changes_the_key(self):
        assert self.key(prompt_template="Find claims: {{ passage_text }}") != self.key()

    def test_editing_the_schema_def_changes_the_key(self):
        changed = {"record_types": [{"id": "claim", "fields": {"q": {"type": "number"}}}]}
        assert self.key(schema_def=changed) != self.key()
        assert self.key(schema_def=None) != self.key()

    def test_the_context_window_changes_the_key(self):
        assert self.key(context_before=2) != self.key()
        assert self.key(context_after=1) != self.key()

    def test_the_entity_hints_change_the_key(self):
        assert self.key(entity_hints="Bultmann") != self.key()
        assert self.key(entity_hints="") != self.key()

    def test_the_model_and_schema_version_change_the_key(self):
        assert self.key(llm_model="model-b") != self.key()
        assert self.key(schema_version=2) != self.key()


class TestContext:
    """Neighbours are shown to the model, never offered as evidence."""

    PROMPT = (
        "Before: {{ context_before }}\nPassage: {{ passage_text }}\n"
        "After: {{ context_after }}\nKnown names: {{ entity_hints }}"
    )

    def build_with_neighbours(self, llm):
        schema = a_schema()
        schema.prompt_template = self.PROMPT
        before = [SimpleNamespace(id=new_id(), text="A sentence from before.")]
        after = [SimpleNamespace(id=new_id(), text="A sentence from after.")]
        return build(llm, schema=schema, before=before, after=after)

    @pytest.mark.asyncio
    async def test_neighbours_and_hints_reach_the_prompt(self):
        llm = FakeLLM(a_good_answer())
        executor, _, passage, _ = self.build_with_neighbours(llm)

        batch = await executor.execute(
            [passage.id],
            "interpretive_claims:1",
            ExtractionOptions(
                context_before=1, context_after=1, entity_hints="Paul, Bultmann"
            ),
        )

        assert batch.results[0].status == ExtractionStatus.ok
        prompt = llm.calls[0]["prompt"]
        assert "A sentence from before." in prompt
        assert "A sentence from after." in prompt
        assert "Paul, Bultmann" in prompt

    @pytest.mark.asyncio
    async def test_evidence_found_only_in_the_context_fails(self):
        """The neighbour's text is not this passage's text.

        Accepting it would anchor the record to offsets that do not address
        the passage it is stored against — a citation that verifies against
        the wrong sentences, or none.
        """
        llm = FakeLLM(a_good_answer("A sentence from before"))
        executor, extractions, passage, _ = self.build_with_neighbours(llm)

        batch = await executor.execute(
            [passage.id],
            "interpretive_claims:1",
            ExtractionOptions(context_before=1, retry_on_validation_error=False),
        )

        assert batch.results[0].status == ExtractionStatus.failed
        [stored] = list(extractions.saved.values())
        assert stored.status == ExtractionStatus.failed

    @pytest.mark.asyncio
    async def test_a_wider_window_is_a_different_extraction(self):
        llm = FakeLLM(a_good_answer(), a_good_answer())
        executor, extractions, passage, _ = self.build_with_neighbours(llm)

        await executor.execute([passage.id], "interpretive_claims:1")
        widened = await executor.execute(
            [passage.id],
            "interpretive_claims:1",
            ExtractionOptions(context_before=1),
        )

        assert widened.results[0].from_cache is False
        assert len(llm.calls) == 2
        assert len(extractions.saved) == 2


class TestImportRecords:
    """Records made elsewhere take the same road in as records made here."""

    @pytest.mark.asyncio
    async def test_imported_records_are_stored_without_calling_a_model(self):
        llm = FakeLLM()
        executor, extractions, passage, schema = build(llm)

        batch = await executor.import_records(
            {passage.id: a_good_answer()["records"]},
            "interpretive_claims:1",
            model="external-batch",
        )

        assert llm.calls == []
        assert batch.results[0].status == ExtractionStatus.ok
        assert batch.results[0].llm_call_id is None
        [stored] = list(extractions.saved.values())
        assert stored.schema_id == schema.id
        assert stored.llm_model == "external-batch"
        assert stored.llm_call_id is None
        [record] = extractions.records[stored.id]
        assert record.record_type == "claim"
        assert PASSAGE_TEXT[record.evidence_start : record.evidence_end] == (
            "forensic rather than transformative"
        )
        assert batch.results[0].records[0]["evidence_spans"] == [
            ["quote", record.evidence_start, record.evidence_end]
        ]

    @pytest.mark.asyncio
    async def test_an_import_honours_the_cache(self):
        executor, extractions, passage, _ = build(FakeLLM())
        records = {passage.id: a_good_answer()["records"]}

        await executor.import_records(records, "interpretive_claims:1", model="m")
        second = await executor.import_records(
            records, "interpretive_claims:1", model="m"
        )
        forced = await executor.import_records(
            records,
            "interpretive_claims:1",
            model="m",
            options=ExtractionOptions(force_refresh=True),
        )

        assert second.results[0].from_cache is True
        assert forced.results[0].from_cache is False
        assert len(extractions.saved) == 1

    @pytest.mark.asyncio
    async def test_an_import_and_a_live_run_share_a_cache(self):
        """Same schema, model, and options: one stored answer, whoever made it."""
        llm = FakeLLM(a_good_answer())
        executor, _, passage, _ = build(llm)

        await executor.import_records(
            {passage.id: a_good_answer()["records"]},
            "interpretive_claims:1",
            model="claude-sonnet-4-5-20250929",
        )
        batch = await executor.execute([passage.id], "interpretive_claims:1")

        assert batch.results[0].from_cache is True
        assert llm.calls == []

    @pytest.mark.asyncio
    async def test_bad_evidence_stores_a_failed_row(self):
        executor, extractions, passage, _ = build(FakeLLM())

        batch = await executor.import_records(
            {passage.id: a_good_answer("text that is not in the passage")["records"]},
            "interpretive_claims:1",
            model="external-batch",
        )

        assert batch.results[0].status == ExtractionStatus.failed
        assert batch.results[0].llm_call_id is None
        [stored] = list(extractions.saved.values())
        assert stored.status == ExtractionStatus.failed
        assert stored.error
        assert extractions.records[stored.id] == []

    @pytest.mark.asyncio
    async def test_a_missing_passage_writes_nothing(self):
        executor, extractions, _, _ = build(FakeLLM())

        batch = await executor.import_records(
            {new_id(): a_good_answer()["records"]},
            "interpretive_claims:1",
            model="external-batch",
        )

        assert batch.results[0].status == ExtractionStatus.failed
        assert extractions.saved == {}
