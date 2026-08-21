"""Fixtures for tests that need a real Postgres.

These skip rather than fail when no database is reachable, so `make test` on a
machine without Docker stays green.

The `Corpus` helper lives in `research_engine.testing` rather than here, because
packs need the same isolation contract and a copied conftest is how it gets
subtly wrong — see the YourCloudLibrary suite, which ingested real books into
the live corpus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from research_engine.config import load_settings
from research_engine.testing import Corpus, CorpusFootprint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["Corpus", "CorpusFootprint"]


@pytest.fixture(scope="session")
def db_url() -> str:
    return load_settings().db_url


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(db_url, pool_pre_ping=True)
    try:
        async with eng.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any connection failure means "no DB"
        await eng.dispose()
        pytest.skip(f"No Postgres at {db_url}: {exc}")
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def corpus(engine: AsyncEngine) -> AsyncIterator[Corpus]:
    """A scratch corpus that removes exactly what it created."""
    helper = Corpus(engine)
    try:
        yield helper
    finally:
        await helper.cleanup()


@pytest.fixture(scope="session")
async def _session_engine(db_url: str) -> AsyncIterator[AsyncEngine | None]:
    eng = create_async_engine(db_url, pool_pre_ping=True)
    try:
        async with eng.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception:  # noqa: BLE001 - no database means nothing to guard
        await eng.dispose()
        yield None
        return
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture(scope="session", autouse=True)
async def corpus_is_unchanged(_session_engine: AsyncEngine | None) -> AsyncIterator[None]:
    """Fail the run if the suite as a whole left anything behind.

    Per-test cleanup is where isolation is supposed to happen, but it only
    covers what a test author remembered to track. This measures every table in
    `core` around the entire session, so a leak shows up as a failure here even
    when no individual test knows it is leaking — which is how 165 events and
    165 edges reached the live corpus while every isolation test passed.
    """
    if _session_engine is None:
        yield
        return
    before = await CorpusFootprint.measure(_session_engine)
    yield
    before.assert_unchanged(await CorpusFootprint.measure(_session_engine))
