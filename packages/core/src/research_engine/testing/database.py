"""Steer test suites at a dedicated database, not the researcher's corpus."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa
import structlog

import research_engine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger()

DEFAULT_TEST_DB_NAME = "research_engine_test"

#: Set to "1" to run a suite against whatever RE_DB_URL points at, including the
#: real corpus. Deliberately awkward: opting in should be a decision, not a
#: default someone inherits by copying a conftest.
ALLOW_REAL_CORPUS = "RE_TEST_ALLOW_REAL_CORPUS"


def resolve_test_db_url(db_url: str | None = None, *, test_db: str | None = None) -> str:
    """Rewrite a connection URL to point at the test database.

    Packs default their integration suites at ``localhost:5435/research_engine``
    — the dev corpus — because that is what is running. This redirects the
    database name while keeping host, port and credentials, so a suite reaches a
    scratch database by default and the real corpus only on explicit opt-in.
    """
    url = db_url or os.environ.get("RE_DB_URL") or (
        "postgresql+asyncpg://re_dev:re_dev_pass@localhost:5435/research_engine"
    )
    if os.environ.get(ALLOW_REAL_CORPUS) == "1":
        return url
    # render_as_string(hide_password=False), not str(): SQLAlchemy masks the
    # password as *** in __str__, which produces a URL that cannot connect.
    return sa.engine.make_url(url).set(
        database=test_db or DEFAULT_TEST_DB_NAME
    ).render_as_string(hide_password=False)


async def ensure_test_database(url: str) -> bool:
    """Create the test database and its schema if absent.

    Returns False when the server is unreachable, so suites can skip rather than
    fail on a machine with no Postgres.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    parsed = sa.engine.make_url(url)
    admin_url = parsed.set(database="postgres").render_as_string(hide_password=False)

    try:
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                exists = (
                    await conn.execute(
                        sa.text("SELECT 1 FROM pg_database WHERE datname = :n"),
                        {"n": parsed.database},
                    )
                ).first()
                if not exists:
                    await conn.execute(sa.text(f'CREATE DATABASE "{parsed.database}"'))
        finally:
            await admin.dispose()
    except Exception:  # noqa: BLE001 - unreachable server means "skip", not "fail"
        return False

    # Build the schema with Alembic, not `metadata.create_all`. The SQLAlchemy
    # metadata is not a faithful description of the real schema — it declares
    # GIN indexes on `json` columns, which Postgres rejects outright and which
    # migration 001 never created. Migrating means the test database is built
    # exactly the way the real one was.
    try:
        await asyncio.to_thread(_run_migrations, url)
    except Exception as exc:  # noqa: BLE001 - report, never swallow silently
        logger.error("test_database_migration_failed", url=_safe(url), error=str(exc))
        return False
    return True


def _run_migrations(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    ini = (
        Path(research_engine.__file__).parent
        / "adapters/storage/postgres/migrations/alembic.ini"
    )
    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent))

    # env.py builds its engine from `load_settings().db_url`, so the target is
    # selected through the environment rather than the Alembic config.
    previous = os.environ.get("RE_DB_URL")
    os.environ["RE_DB_URL"] = url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("RE_DB_URL", None)
        else:
            os.environ["RE_DB_URL"] = previous


def _safe(url: str) -> str:
    """A connection URL with the password masked, for logs."""
    return str(sa.engine.make_url(url))


@dataclass
class CorpusFootprint:
    """Row counts used to assert a suite left nothing behind.

    Every table in the ``core`` schema, not a chosen few. The earlier version
    counted documents, passages and embeddings, which meant a suite could leave
    anything else behind and this would report the corpus unchanged — and one
    did: ``test_reindex`` plants an event and an edge per run, neither cascades
    with the document, and 165 of each accumulated in the live corpus over nine
    days while every isolation test passed.

    Counting everything also means a table added by a later migration is covered
    without anyone remembering to add it here.
    """

    counts: dict[str, int]

    #: Named accessors for the three that callers ask about directly.
    @property
    def documents(self) -> int:
        return self.counts.get("documents", 0)

    @property
    def passages(self) -> int:
        return self.counts.get("passages", 0)

    @property
    def embeddings(self) -> int:
        return self.counts.get("passage_embeddings", 0)

    @classmethod
    async def measure(cls, engine: AsyncEngine) -> CorpusFootprint:
        async with engine.connect() as conn:
            names = [
                row[0]
                for row in (
                    await conn.execute(
                        sa.text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'core' AND table_type = 'BASE TABLE' "
                            "AND table_name <> 'alembic_version' ORDER BY table_name"
                        )
                    )
                ).all()
            ]
            counts = {}
            for name in names:
                counts[name] = (
                    await conn.execute(
                        sa.text(f'SELECT count(*) FROM core."{name}"')  # noqa: S608
                    )
                ).scalar_one()
        return cls(counts)

    def assert_unchanged(self, other: CorpusFootprint) -> None:
        """Raise if a suite added or removed rows.

        The contract every pack's integration suite must satisfy: run it twice,
        and the corpus is exactly as it was.
        """
        drift = {
            name: (before, other.counts.get(name, 0))
            for name, before in self.counts.items()
            if before != other.counts.get(name, 0)
        }
        drift.update({
            name: (0, count)
            for name, count in other.counts.items()
            if name not in self.counts and count
        })
        if drift:
            detail = ", ".join(
                f"{k}: {b} -> {a}" for k, (b, a) in sorted(drift.items())
            )
            raise AssertionError(
                f"Integration suite changed the corpus ({detail}). Tests must "
                f"remove exactly the rows they create — use "
                f"research_engine.testing.Corpus, and `Corpus.track` for rows "
                f"that do not cascade with a document."
            )
