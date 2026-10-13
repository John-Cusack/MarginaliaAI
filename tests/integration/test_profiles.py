"""Reader profiles, their source roles and runs round-trip through the database.

A profile gives each edition a role over a date range; one edition can hold
different roles for different readers, and several roles for one reader, but
never the same role twice over the same dates. Runs record exactly which
profile data and config they ran under.
"""

from __future__ import annotations

import uuid
from functools import partial
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from research_engine.adapters.profile_client import ProfileServiceAdapter, config_hash
from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGEditionRepo,
    PGProfileRepo,
    PGRunRepo,
)
from research_engine.adapters.storage.postgres.schema import runs
from research_engine.domain.errors import NotFoundError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

HASH_A = "a" * 64
HASH_B = "b" * 64


def key(prefix: str = "test") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def client(engine: AsyncEngine) -> ProfileServiceAdapter:
    return ProfileServiceAdapter(
        editions=PGEditionRepo(engine),
        profiles=PGProfileRepo(engine),
        runs=PGRunRepo(engine),
        transaction_factory=partial(transaction, engine),
    )


async def edition(engine: AsyncEngine, corpus: Corpus) -> str:
    edition_key = f"TEST-{uuid.uuid4().hex[:10]}"
    async with transaction(engine) as tx:
        row = await PGEditionRepo(engine).upsert_key(tx, edition_key)
    corpus.adopt_edition(row.id)
    return edition_key


async def load(
    client: ProfileServiceAdapter, corpus: Corpus, profile_key: str, roles, **kwargs
) -> dict:
    result = await client.load_profile(
        profile_key, version=kwargs.pop("version", 1),
        profile_hash=kwargs.pop("profile_hash", HASH_A), roles=roles, **kwargs,
    )
    corpus.adopt_profile(uuid.UUID(result["id"]))
    return result


async def test_one_edition_holds_different_roles_for_different_readers(
    client: ProfileServiceAdapter, engine: AsyncEngine, corpus: Corpus
) -> None:
    bible = await edition(engine, corpus)
    first, second = key(), key()

    await load(client, corpus, first, [{"edition_key": bible, "role": "binding", "tier": 1}])
    await load(
        client, corpus, second,
        [{"edition_key": bible, "role": "external",
          "scope": {"books": ["Genesis", "Exodus"]}}],
    )

    one = await client.get_profile(first)
    two = await client.get_profile(second)
    assert [(r["role"], r["tier"]) for r in one["roles"]] == [("binding", 1)]
    assert [(r["role"], r["scope"]) for r in two["roles"]] == [
        ("external", {"books": ["Genesis", "Exodus"]})
    ]


async def test_roles_hold_over_windows_and_cannot_overlap(
    client: ProfileServiceAdapter, engine: AsyncEngine, corpus: Corpus
) -> None:
    """Lectures bound into a canon in 1835 and dropped from it in 1921."""
    lectures = await edition(engine, corpus)
    profile_key = key()
    canon = {"edition_key": lectures, "role": "binding", "tier": 1,
             "valid_from": "1835-08-17", "valid_to": "1921-01-01"}
    after = {"edition_key": lectures, "role": "reference", "tier": 4,
             "valid_from": "1921-01-01"}

    await load(client, corpus, profile_key, [canon, after])
    in_1900 = await client.get_profile(profile_key, on="1900-01-01")
    in_1950 = await client.get_profile(profile_key, on="1950-01-01")
    assert [r["role"] for r in in_1900["roles"]] == ["binding"]
    assert [r["role"] for r in in_1950["roles"]] == ["reference"]

    with pytest.raises(ValueError, match="same role twice"):
        await client.load_profile(
            profile_key, version=2, profile_hash=HASH_B,
            roles=[canon, {**canon, "valid_from": "1900-01-01", "valid_to": None}],
        )
    # The refused load changed nothing.
    kept = await client.get_profile(profile_key)
    assert kept["version"] == 1 and len(kept["roles"]) == 2

    with pytest.raises(ValueError, match="not after"):
        await client.load_profile(
            profile_key, version=2, profile_hash=HASH_B,
            roles=[{**canon, "valid_from": "1921-01-01", "valid_to": "1835-01-01"}],
        )


async def test_an_edition_may_hold_two_roles_at_once(
    client: ProfileServiceAdapter, engine: AsyncEngine, corpus: Corpus
) -> None:
    """An essay that binds the reader and also answers its critics."""
    essay = await edition(engine, corpus)
    profile_key = key()
    await load(
        client, corpus, profile_key,
        [{"edition_key": essay, "role": "binding", "tier": 2},
         {"edition_key": essay, "role": "defense", "tier": 5}],
    )
    stored = await client.get_profile(profile_key)
    assert sorted(r["role"] for r in stored["roles"]) == ["binding", "defense"]


async def test_reloading_replaces_roles_whole(
    client: ProfileServiceAdapter, engine: AsyncEngine, corpus: Corpus
) -> None:
    a, b = await edition(engine, corpus), await edition(engine, corpus)
    profile_key = key()
    first = await load(
        client, corpus, profile_key,
        [{"edition_key": a, "role": "binding"}, {"edition_key": b, "role": "critical"}],
    )
    second = await client.load_profile(
        profile_key, version=2, profile_hash=HASH_B,
        roles=[{"edition_key": a, "role": "binding"}],
    )
    assert second["id"] == first["id"]
    stored = await client.get_profile(profile_key)
    assert stored["profile_hash"] == HASH_B
    assert [r["edition_key"] for r in stored["roles"]] == [a]


async def test_a_profile_cannot_name_an_unregistered_edition(
    client: ProfileServiceAdapter,
) -> None:
    with pytest.raises(NotFoundError, match="registered edition not found"):
        await client.load_profile(
            key(), version=1, profile_hash=HASH_A,
            roles=[{"edition_key": f"TEST-missing-{uuid.uuid4().hex}", "role": "binding"}],
        )
    with pytest.raises(ValueError):
        await client.load_profile(
            key(), version=1, profile_hash=HASH_A,
            roles=[{"edition_key": "x", "role": "canon"}],
        )


async def test_a_run_records_what_it_ran_under(
    client: ProfileServiceAdapter, engine: AsyncEngine, corpus: Corpus
) -> None:
    bible = await edition(engine, corpus)
    profile_key = key()
    await load(client, corpus, profile_key, [{"edition_key": bible, "role": "binding"}])
    config = {"tau": 0.5, "k": 50}

    run = await client.start_run(
        profile_key, stage="ingest", config=config, items_in=3, cost_estimate=1.25
    )
    assert run["status"] == "running"
    assert run["profile_hash"] == HASH_A
    assert run["config_hash"] == config_hash({"k": 50, "tau": 0.5})

    # Loading new profile data later does not rewrite what the run ran under.
    await client.load_profile(
        profile_key, version=2, profile_hash=HASH_B,
        roles=[{"edition_key": bible, "role": "binding"}],
    )
    done = await client.finish_run(run["id"], items_out=2, stats={"refused": 1})
    assert done["status"] == "finished"
    assert done["profile_hash"] == HASH_A
    assert done["items_out"] == 2 and done["finished_at"] is not None

    with pytest.raises(ValueError, match="already closed"):
        await client.finish_run(run["id"], status="failed")

    with pytest.raises(sa.exc.IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                runs.update()
                .where(runs.c.id == uuid.UUID(run["id"]))
                .values(finished_at=None)
            )
