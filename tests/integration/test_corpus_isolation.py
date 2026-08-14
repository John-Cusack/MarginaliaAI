"""The isolation contract every integration suite must satisfy.

A test suite that adds rows to the corpus it runs against is not a test suite,
it is an ingest. This is the guard that would have caught the YourCloudLibrary
plugin writing 12 real books and 2,095 stub embeddings into the live corpus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from research_engine.testing import Corpus, CorpusFootprint, resolve_test_db_url

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration]


async def test_corpus_helper_leaves_no_trace(engine: AsyncEngine) -> None:
    before = await CorpusFootprint.measure(engine)

    helper = Corpus(engine)
    doc = await helper.add_document(title="scratch")
    await helper.add_passage(doc, "Some text that should not survive this test.")
    await helper.cleanup()

    after = await CorpusFootprint.measure(engine)
    before.assert_unchanged(after)


async def test_footprint_detects_a_leak(engine: AsyncEngine) -> None:
    """The guard must actually fire, or it is decoration."""
    before = await CorpusFootprint.measure(engine)

    leaky = Corpus(engine)
    doc = await leaky.add_document(title="leaked")
    await leaky.add_passage(doc, "Left behind on purpose.")

    after = await CorpusFootprint.measure(engine)
    with pytest.raises(AssertionError, match="changed the corpus"):
        before.assert_unchanged(after)

    await leaky.cleanup()
    assert (await CorpusFootprint.measure(engine)).documents == before.documents


async def test_adopt_tracks_documents_created_by_the_code_under_test(
    engine: AsyncEngine,
) -> None:
    """A suite exercising the real ingest path must still clean up after it."""
    before = await CorpusFootprint.measure(engine)

    helper = Corpus(engine)
    # Stand in for an ingest that returns a document id the test did not create.
    other = Corpus(engine)
    doc = await other.add_document(title="ingested by the code under test")
    other._document_ids.clear()  # noqa: SLF001 - simulate a foreign creator

    helper.adopt(doc)
    await helper.cleanup()

    before.assert_unchanged(await CorpusFootprint.measure(engine))


class TestTestDatabaseRouting:
    def test_defaults_away_from_the_real_corpus(self) -> None:
        url = resolve_test_db_url(
            "postgresql+asyncpg://re_dev:pw@localhost:5435/research_engine"
        )
        assert url.endswith("/research_engine_test")
        assert "localhost:5435" in url
        assert "re_dev:pw" in url

    def test_opt_in_is_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RE_TEST_ALLOW_REAL_CORPUS", "1")
        url = resolve_test_db_url(
            "postgresql+asyncpg://re_dev:pw@localhost:5435/research_engine"
        )
        assert url.endswith("/research_engine")
