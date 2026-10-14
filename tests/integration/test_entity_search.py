"""Entity search by name, against a real Postgres.

Two behaviours exist only in the SQL and so can only be proved here: fuzzy
search scores an entity by its best *alias* as well as its canonical name
(the alias is often the form a text actually uses), and exact search is
case-insensitive equality against either — nothing merely similar counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import PGEntityRepo
from research_engine.adapters.storage.postgres.schema import entities
from research_engine.domain.entities import EntityAlias, EntityDraft

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

CANONICAL = "Zebediah Quillfeather"
ALIAS = "Zeb Q"


@pytest.fixture
async def quillfeather(engine: AsyncEngine, corpus: Corpus) -> PGEntityRepo:
    repo = PGEntityRepo(engine)
    async with transaction(engine) as tx:
        entity = await repo.insert(
            tx, EntityDraft(entity_type="person", canonical_name=CANONICAL)
        )
        await repo.add_alias(tx, EntityAlias(entity_id=entity.id, alias=ALIAS))
    corpus.track(entities, entity.id)
    return repo


class TestFuzzySearch:
    async def test_an_alias_finds_its_entity(
        self, quillfeather: PGEntityRepo
    ) -> None:
        candidates = await quillfeather.search_by_name(ALIAS, "person", 5)

        assert [c.canonical_name for c in candidates][0] == CANONICAL
        assert candidates[0].match_score > 0.9
        assert ALIAS in candidates[0].aliases

    async def test_the_canonical_name_still_finds_its_entity(
        self, quillfeather: PGEntityRepo
    ) -> None:
        candidates = await quillfeather.search_by_name(CANONICAL, "person", 5)

        assert candidates[0].canonical_name == CANONICAL


class TestExactSearch:
    async def test_equality_is_case_insensitive_on_name_and_alias(
        self, quillfeather: PGEntityRepo
    ) -> None:
        for query in (CANONICAL.lower(), ALIAS.upper()):
            candidates = await quillfeather.search_by_name(
                query, "person", 5, exact=True
            )
            assert [c.canonical_name for c in candidates] == [CANONICAL]
            assert candidates[0].match_score == 1.0

    async def test_a_near_miss_is_not_a_match(
        self, quillfeather: PGEntityRepo
    ) -> None:
        candidates = await quillfeather.search_by_name(
            "Zebediah", "person", 5, exact=True
        )

        assert CANONICAL not in [c.canonical_name for c in candidates]

    async def test_exact_search_still_filters_by_type(
        self, quillfeather: PGEntityRepo
    ) -> None:
        candidates = await quillfeather.search_by_name(
            CANONICAL, "place", 5, exact=True
        )

        assert candidates == []
