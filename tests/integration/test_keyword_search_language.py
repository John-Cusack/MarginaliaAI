"""Per-language keyword search against a real Postgres.

The unit tests assert the SQL is shaped right. These assert Postgres agrees:
that the union form executes, that each language is stemmed by its own
stemmer, and that the GIN index is still reachable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.passages import (
    PGPassageRepo,
    build_keyword_search_sql,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

GERMAN = "Die Häuser in der Altstadt waren sehr alt."
ENGLISH = "The scholars were running experiments in the laboratory."


async def _index(engine: AsyncEngine, repo: PGPassageRepo, pid, text: str, cfg: str) -> None:
    async with transaction(engine) as tx:
        await repo.index_fts(tx, [pid], [text], cfg)


async def test_each_language_is_stemmed_by_its_own_stemmer(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    repo = PGPassageRepo(engine)

    de_doc = await corpus.add_document(language="de")
    de_passage = await corpus.add_passage(de_doc, GERMAN)
    await _index(engine, repo, de_passage, GERMAN, "german")

    en_doc = await corpus.add_document(language="en")
    en_passage = await corpus.add_passage(en_doc, ENGLISH)
    await _index(engine, repo, en_passage, ENGLISH, "english")

    # "Haus" only reaches "Häuser" through the German stemmer.
    hits = await repo.keyword_search("Haus", None, [de_passage, en_passage], 10)
    assert [pid for pid, _ in hits] == [de_passage]

    # "run" only reaches "running" through the English stemmer.
    hits = await repo.keyword_search("run", None, [de_passage, en_passage], 10)
    assert [pid for pid, _ in hits] == [en_passage]


async def test_english_stemmer_does_not_find_german_inflection(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """The bug this phase fixes: German indexed under English is unfindable."""
    repo = PGPassageRepo(engine)
    doc = await corpus.add_document(language="de")
    passage = await corpus.add_passage(doc, GERMAN)

    await _index(engine, repo, passage, GERMAN, "english")
    assert await repo.keyword_search("Haus", "english", [passage], 10) == []

    # Re-indexing under German makes it findable — and the upsert must move
    # lang_config across with the vector, or the row stays routed to English.
    await _index(engine, repo, passage, GERMAN, "german")
    hits = await repo.keyword_search("Haus", None, [passage], 10)
    assert [pid for pid, _ in hits] == [passage]


async def test_reindex_updates_lang_config_not_just_the_vector(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    repo = PGPassageRepo(engine)
    doc = await corpus.add_document(language="de")
    passage = await corpus.add_passage(doc, GERMAN)

    await _index(engine, repo, passage, GERMAN, "english")
    await _index(engine, repo, passage, GERMAN, "german")

    async with engine.connect() as conn:
        cfg = (
            await conn.execute(
                sa.text(
                    "SELECT lang_config::text AS c FROM core.passage_fts "
                    "WHERE passage_id = :pid"
                ),
                {"pid": passage},
            )
        ).scalar_one()
    assert cfg == "german"


async def test_explicit_language_restricts_to_one_branch(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    repo = PGPassageRepo(engine)
    doc = await corpus.add_document(language="de")
    passage = await corpus.add_passage(doc, GERMAN)
    await _index(engine, repo, passage, GERMAN, "german")

    assert await repo.keyword_search("Haus", "german", [passage], 10) != []
    assert await repo.keyword_search("Haus", "english", [passage], 10) == []


async def test_simple_config_does_not_stem(engine: AsyncEngine, corpus: Corpus) -> None:
    """Why `simple` is the safe fallback: it under-matches, never mis-matches."""
    repo = PGPassageRepo(engine)
    doc = await corpus.add_document()
    passage = await corpus.add_passage(doc, ENGLISH)
    await _index(engine, repo, passage, ENGLISH, "simple")

    assert await repo.keyword_search("run", "simple", [passage], 10) == []
    assert await repo.keyword_search("running", "simple", [passage], 10) != []


async def test_union_query_can_use_the_gin_index(engine: AsyncEngine) -> None:
    """The per-row `plainto_tsquery(pf.lang_config, ...)` form cannot use
    passage_fts_ts_idx at any table size. This one can.

    Sequential scan is disabled so the planner reveals what it *could* use,
    rather than what it prefers on a near-empty table.
    """
    sql = build_keyword_search_sql(["english", "german"])
    async with engine.connect() as conn:
        await conn.execute(sa.text("SET LOCAL enable_seqscan = off"))
        rows = await conn.execute(
            sa.text(f"EXPLAIN {sql}"),
            {"query": "test", "no_filter": True, "candidate_ids": [], "k": 10},
        )
        plan = "\n".join(row[0] for row in rows)

    assert "passage_fts_ts_idx" in plan, plan
