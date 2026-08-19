"""Extraction, end to end, against a real Postgres.

Everything asserted here was true of the code and false of the database: the
executor validated, retried, and returned records, and wrote none of them. So
`extractions` and `extraction_records` were empty by construction, the cache
could not hit, and no extracted claim could be traced back to the text it came
from.

The LLM is a stub that quotes the passage back with its whitespace mangled — the
way a model re-typing a quotation does. That is deliberate: what is under test
is the storage and anchoring path, and whitespace tolerance is exactly where
anchoring goes quietly wrong.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import sqlalchemy as sa

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.extractions import (
    PGExtractionRepo,
    PGExtractionSchemaRepo,
)
from research_engine.adapters.storage.postgres.repositories.passages import (
    PGPassageRepo,
)
from research_engine.adapters.storage.postgres.schema import (
    extraction_records,
    extraction_schemas,
    extractions,
)
from research_engine.domain.extractions import ExtractionOptions
from research_engine.services.extraction.executor import ExtractionExecutor
from research_engine.services.extraction.registration import ExtractionSchemaService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = pytest.mark.asyncio

PASSAGE = (
    "Paul's use of dikaiosyne theou in Romans 3:21 is forensic rather than "
    "transformative, and Habakkuk 2:4 is invoked in support of that reading."
)

SCHEMA_YAML = """
id: roundtrip_claims
version: 1
owner: test_roundtrip
record_types:
  - id: claim
    fields:
      assertion:
        type: string
        required: true
      quote:
        type: evidence_span
        required: true
      stance:
        type: enum
        values: [affirms, denies]
prompt: |
  Extract claims from this passage.

  {{ passage_text }}
"""


class StubLLM:
    """Quotes words 5-11 of the passage back, re-typed rather than copied."""

    def __init__(self) -> None:
        self.calls = 0

    async def structured(self, messages, schema, model=None, caller="core", purpose=""):
        self.calls += 1
        passage_text = messages[0]["content"].split("\n\n", 1)[1].strip()
        quote = "  \n  ".join(passage_text.split()[4:11])
        return {
            "records": [
                {
                    "record_type": "claim",
                    "fields": {
                        "assertion": "the phrase is read forensically",
                        "quote": quote,
                        "stance": "affirms",
                    },
                }
            ]
        }, uuid4()

    async def complete(self, *args, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture
async def extraction_setup(engine: AsyncEngine, corpus: Corpus):
    schema_repo = PGExtractionSchemaRepo(engine)
    service = ExtractionSchemaService(
        schema_repo, lambda: transaction(engine)
    )
    schema = await service.register_yaml(SCHEMA_YAML)
    # extraction_schemas outlives its documents and has a unique key, so an
    # untracked one makes the *next* run fail rather than this one.
    corpus.track(extraction_schemas, schema.id)

    document_id = await corpus.add_document(title="roundtrip")
    passage_id = await corpus.add_passage(document_id, PASSAGE)

    llm = StubLLM()
    executor = ExtractionExecutor(
        llm=llm,
        passages=PGPassageRepo(engine),
        extractions=PGExtractionRepo(engine),
        extraction_schemas=schema_repo,
        transaction_factory=lambda: transaction(engine),
        default_model="stub-model",
    )
    return executor, llm, schema, passage_id


async def test_a_run_is_stored(engine: AsyncEngine, extraction_setup):
    executor, _, schema, passage_id = extraction_setup

    batch = await executor.execute([passage_id], "roundtrip_claims:1")

    assert [r.status for r in batch.results] == ["ok"]
    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                extractions.select().where(extractions.c.schema_id == schema.id)
            )
        ).all()
    assert len(stored) == 1
    assert stored[0].status == "ok"
    assert stored[0].llm_model == "stub-model"


async def test_the_stored_offsets_slice_the_passage(
    engine: AsyncEngine, extraction_setup
):
    """The whole point: a claim you can get back to the sentence for."""
    executor, _, schema, passage_id = extraction_setup

    await executor.execute([passage_id], "roundtrip_claims:1")

    async with engine.connect() as conn:
        quoted = (
            await conn.execute(
                sa.text(
                    "SELECT substring(p.text FROM r.evidence_start + 1 "
                    "FOR r.evidence_end - r.evidence_start) "
                    "FROM core.extraction_records r "
                    "JOIN core.passages p ON p.id = r.passage_id "
                    "WHERE r.schema_id = :schema_id"
                ),
                {"schema_id": schema.id},
            )
        ).scalar()
    assert quoted == "theou in Romans 3:21 is forensic rather"


async def test_a_second_run_is_served_from_the_database(extraction_setup):
    executor, llm, _, passage_id = extraction_setup

    await executor.execute([passage_id], "roundtrip_claims:1")
    second = await executor.execute([passage_id], "roundtrip_claims:1")

    assert llm.calls == 1
    assert second.results[0].from_cache is True


async def test_re_running_replaces_rather_than_accumulates(
    engine: AsyncEngine, extraction_setup
):
    """`(passage, schema, extractor_version)` is unique; a re-run must upsert."""
    executor, _, schema, passage_id = extraction_setup

    await executor.execute([passage_id], "roundtrip_claims:1")
    await executor.execute(
        [passage_id], "roundtrip_claims:1", ExtractionOptions(force_refresh=True)
    )

    async with engine.connect() as conn:
        n_extractions = (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(extractions)
                .where(extractions.c.schema_id == schema.id)
            )
        ).scalar()
        n_records = (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(extraction_records)
                .where(extraction_records.c.schema_id == schema.id)
            )
        ).scalar()
    assert (n_extractions, n_records) == (1, 1)


async def test_records_are_queryable_by_their_data(
    engine: AsyncEngine, extraction_setup
):
    """`data` is a `json` column and `@>` is a `jsonb` operator.

    The query path cast neither, and folded the passage filter into the same
    dict, so it asked whether a claim contained a key named "passage_filter".
    """
    executor, _, _, passage_id = extraction_setup
    await executor.execute([passage_id], "roundtrip_claims:1")
    repo = PGExtractionRepo(engine)

    hits = await repo.query_records("claim", data_filter={"stance": "affirms"})
    misses = await repo.query_records("claim", data_filter={"stance": "denies"})
    scoped = await repo.query_records("claim", passage_ids=[passage_id])

    assert len(hits) == 1
    assert misses == []
    assert len(scoped) == 1


async def test_a_record_can_be_fetched_by_its_own_id(
    engine: AsyncEngine, extraction_setup
):
    """What `provenance_of` needs, and asked for through a filter that could
    never match."""
    executor, _, _, passage_id = extraction_setup
    await executor.execute([passage_id], "roundtrip_claims:1")
    repo = PGExtractionRepo(engine)
    [record] = await repo.query_records("claim", passage_ids=[passage_id])

    assert (await repo.get_record(record.id)).id == record.id
    assert await repo.get_record(uuid4()) is None
