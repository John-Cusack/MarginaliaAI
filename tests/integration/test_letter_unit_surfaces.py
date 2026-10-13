"""The storage paths a pack needs to split a collected volume into letters.

A letter's date is decided after it has been split out and revised on review,
so a document's description has to be updatable without touching its content;
a pack has to find the letters it made by their metadata; it has to read a
volume's own text and its passages' offsets into it; and it has to read one
extraction's records, not every run's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from research_engine.adapters.corpus_client import CorpusServiceAdapter
from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.document_texts import (
    PGDocumentTextRepo,
)
from research_engine.adapters.storage.postgres.repositories.documents import PGDocumentRepo
from research_engine.adapters.storage.postgres.repositories.extractions import (
    PGExtractionRepo,
)
from research_engine.adapters.storage.postgres.repositories.passages import PGPassageRepo
from research_engine.adapters.storage.postgres.schema import (
    extraction_records,
    extraction_schemas,
    extractions,
)
from research_engine.testing.corpus import new_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

GENEVA = datetime(1814, 7, 1, tzinfo=UTC)


async def test_a_letter_is_dated_after_it_is_split_out(engine: AsyncEngine, corpus: Corpus):
    repo = PGDocumentRepo(engine)
    letter = await corpus.add_document(
        document_type="letter", metadata={"review_status": "needs_review"}
    )

    stored = await repo.update_fields(
        letter,
        {
            "created_date_start": GENEVA,
            "created_date_end": GENEVA.replace(hour=23, minute=59, second=59),
            "created_precision": "day",
        },
        metadata_patch={"review_status": "accepted"},
    )

    assert stored is not None
    assert stored.created_date_start == GENEVA
    assert stored.metadata == {"review_status": "accepted"}


async def test_a_date_can_be_taken_away_again(engine: AsyncEngine, corpus: Corpus):
    repo = PGDocumentRepo(engine)
    letter = await corpus.add_document(document_type="letter")
    await repo.update_fields(letter, {"created_date_start": GENEVA})

    stored = await repo.update_fields(letter, {"created_date_start": None})

    assert stored is not None
    assert stored.created_date_start is None


async def test_content_and_identity_are_not_updatable(engine: AsyncEngine, corpus: Corpus):
    letter = await corpus.add_document(document_type="letter")
    with pytest.raises(ValueError, match="not updatable: source"):
        await PGDocumentRepo(engine).update_fields(letter, {"source": "elsewhere"})


async def test_a_missing_document_updates_nothing(engine: AsyncEngine):
    assert await PGDocumentRepo(engine).update_fields(new_id(), {"title": "x"}) is None


async def test_letters_are_found_by_their_metadata(engine: AsyncEngine, corpus: Corpus):
    volume = new_id()
    held = await corpus.add_document(
        document_type="letter",
        metadata={"parent_document_id": str(volume), "review_status": "needs_review"},
    )
    await corpus.add_document(
        document_type="letter",
        metadata={"parent_document_id": str(volume), "review_status": "auto"},
    )
    client = CorpusServiceAdapter(None, PGDocumentRepo(engine), PGPassageRepo(engine))

    found = await client.find_documents(
        document_types=["letter"],
        metadata={"parent_document_id": str(volume), "review_status": "needs_review"},
    )

    assert [doc["id"] for doc in found] == [str(held)]


async def test_listing_the_whole_corpus_is_refused(engine: AsyncEngine):
    client = CorpusServiceAdapter(None, PGDocumentRepo(engine), PGPassageRepo(engine))
    with pytest.raises(ValueError, match="does not list the whole corpus"):
        await client.find_documents()


async def test_a_volume_s_text_and_its_passage_offsets(engine: AsyncEngine, corpus: Corpus):
    text = "FARADAY TO HIS MOTHER.\n\n' Geneva : July 1. Received July 18."
    volume = await corpus.add_document(document_type="letter_collection")
    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(tx, volume, text, "test", "1.0")
    await corpus.add_passage(volume, text[24:], char_start=24, position=1)
    client = CorpusServiceAdapter(
        None, PGDocumentRepo(engine), PGPassageRepo(engine), None, PGDocumentTextRepo(engine)
    )

    document = await client.get_document(volume)

    assert await client.get_document_text(volume) == text
    [passage] = document["passages"]
    assert (passage["char_start"], passage["char_end"]) == (24, len(text))
    assert text[passage["char_start"] : passage["char_end"]] == passage["text"]


async def test_a_re_extraction_replaces_what_is_read(engine: AsyncEngine, corpus: Corpus):
    """Two runs over one passage are two extraction rows; read the newer only."""
    document = await corpus.add_document()
    passage = await corpus.add_passage(document, "FARADAY TO HIS MOTHER.")
    schema_id = new_id()
    async with engine.begin() as conn:
        await conn.execute(
            extraction_schemas.insert().values(
                id=schema_id,
                name=f"letter_openings_test_{schema_id.hex[:8]}",
                version=1,
                owner="test",
                schema={},
                prompt_template="{{ passage_text }}",
            )
        )
    corpus.track(extraction_schemas, schema_id)
    for version, created, opening in [
        ("old", datetime(2026, 1, 1, tzinfo=UTC), "first reading"),
        ("new", datetime(2026, 2, 1, tzinfo=UTC), "second reading"),
    ]:
        extraction_id = new_id()
        async with engine.begin() as conn:
            await conn.execute(
                extractions.insert().values(
                    id=extraction_id,
                    passage_id=passage,
                    schema_id=schema_id,
                    extractor_version=version,
                    llm_model="test",
                    status="ok",
                    records=[],
                    created_at=created,
                )
            )
            await conn.execute(
                extraction_records.insert().values(
                    id=new_id(),
                    extraction_id=extraction_id,
                    passage_id=passage,
                    schema_id=schema_id,
                    record_type="letter_opening",
                    data={"opening": opening},
                )
            )

    repo = PGExtractionRepo(engine)
    every = await repo.query_records("letter_opening", passage_ids=[passage], k=10)
    latest = await repo.query_records(
        "letter_opening", passage_ids=[passage], k=10, schema_id=schema_id, latest_only=True
    )

    assert len(every) == 2
    assert [record.data["opening"] for record in latest] == ["second reading"]
    async with engine.connect() as conn:
        assert (
            await conn.execute(
                sa.select(sa.func.count()).select_from(extractions).where(
                    extractions.c.passage_id == passage
                )
            )
        ).scalar_one() == 2
