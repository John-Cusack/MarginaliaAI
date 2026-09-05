"""The span table and the ledger, against real Postgres.

The resolver is the whole difference between an identity join and span-overlap
geometry: one row per coordinates no matter how many writers race on them, the
stored slice byte-identical to the canonical text, and a staleness query — not
a column — for when the parser moves underneath. The ledger tables carry the
constraints the guide promises: RESTRICT where deletion must refuse, and a
check that `not_found` is an answer but never a row.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGDocumentRepo,
    PGDocumentTextRepo,
    PGPassageRepo,
    PGSourceSpanRepo,
)
from research_engine.adapters.storage.postgres.schema import (
    anchors,
    claims,
    source_spans,
)
from research_engine.domain.passages import PassageDraft
from research_engine.testing import new_id

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

_FIXTURES = Path(__file__).parent / "fixtures" / "works"
TEXT = (_FIXTURES / "lexicon_fixture.txt").read_text(encoding="utf-8")
SIDECAR = json.loads((_FIXTURES / "lexicon_fixture.json").read_text(encoding="utf-8"))


async def _ingest(engine: AsyncEngine, corpus: Corpus) -> UUID:
    doc_id = await corpus.add_document(title="Dabaris")
    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(tx, doc_id, TEXT, "test", "1.0")
        await PGPassageRepo(engine).insert_many(
            tx,
            doc_id,
            [
                PassageDraft(
                    position=index,
                    char_start=part["char_start"],
                    char_end=part["char_end"],
                    text=TEXT[part["char_start"] : part["char_end"]],
                    chunker="test",
                    chunker_version="1.0",
                )
                for index, part in enumerate(SIDECAR["passages"])
            ],
        )
    return doc_id


@pytest.mark.asyncio
async def test_resolver_is_idempotent(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)
    repo = PGSourceSpanRepo(engine)

    first = await corpus.add_span(doc_id, 34, 62)
    second = await corpus.add_span(doc_id, 34, 62)

    assert first.id == second.id
    assert len(await repo.for_document(doc_id)) == 1


@pytest.mark.asyncio
async def test_concurrent_resolves_converge_on_one_row(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)
    repo = PGSourceSpanRepo(engine)

    first, second = await asyncio.gather(
        corpus.add_span(doc_id, 34, 62), corpus.add_span(doc_id, 34, 62)
    )

    assert first.id == second.id
    assert len(await repo.for_document(doc_id)) == 1


@pytest.mark.asyncio
async def test_stored_slice_is_the_canonical_text(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """`quoted_text` is the slice, byte for byte — the caller passes no text."""
    doc_id = await _ingest(engine, corpus)

    span = await corpus.add_span(doc_id, 103, 152)

    assert span.quoted_text == TEXT[103:152]
    assert span.parser == "test"
    assert span.parser_version == "1.0"
    # Best overlap of [103, 152) is the first passage ([0, 130): 27 chars beat
    # the second's 22), cached on the row.
    passages = await PGPassageRepo(engine).get_by_document(doc_id)
    widest = max(
        passages,
        key=lambda p: min(p.char_end, 152) - max(p.char_start, 103),
    )
    assert span.passage_id == widest.id


@pytest.mark.asyncio
async def test_stale_spans_surface_when_the_parser_moves(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)
    span = await corpus.add_span(doc_id, 34, 62)
    fresh = [s.id for s in await PGSourceSpanRepo(engine).stale()]
    assert span.id not in fresh

    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(tx, doc_id, TEXT, "test", "2.0")

    stale_ids = [s.id for s in await PGSourceSpanRepo(engine).stale()]
    assert span.id in stale_ids


@pytest.mark.asyncio
async def test_a_cited_document_cannot_be_deleted(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)
    span = await corpus.add_span(doc_id, 34, 62)

    with pytest.raises(IntegrityError):
        await PGDocumentRepo(engine).delete(doc_id)

    # Deleting the span first succeeds, and then so does the document.
    async with engine.begin() as conn:
        await conn.execute(source_spans.delete().where(source_spans.c.id == span.id))
    await PGDocumentRepo(engine).delete(doc_id)


@pytest.mark.asyncio
async def test_an_anchor_cannot_store_not_found(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)
    span = await corpus.add_span(doc_id, 34, 62)
    claim_id = new_id()
    async with engine.begin() as conn:
        await conn.execute(
            claims.insert().values(
                id=claim_id, ref="TEST-001", statement="words pair", kind="premise"
            )
        )
    try:
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    anchors.insert().values(
                        id=new_id(),
                        claim_id=claim_id,
                        role="supports",
                        source_span_id=span.id,
                        quoted_text="a fine sentence here",
                        verify_status="not_found",
                    )
                )
    finally:
        async with engine.begin() as conn:
            await conn.execute(claims.delete().where(claims.c.id == claim_id))


@pytest.mark.asyncio
async def test_migrations_revert_cleanly(
    engine: AsyncEngine, db_url: str
) -> None:
    """009 and 010 downgrade away entirely, then come back.

    The downgrade drops tables, so it refuses to run over data: a leaked span
    row would be destroyed rather than reported, and destroying evidence to
    test a migration is backwards.
    """
    for schema, table in (
        ("evidence", "source_spans"),
        ("argument", "claims"),
        ("argument", "claim_edges"),
        ("argument", "anchors"),
    ):
        async with engine.connect() as conn:
            count = (
                await conn.execute(sa.text(f'SELECT count(*) FROM "{schema}"."{table}"'))
            ).scalar_one()
        assert count == 0, f"{schema}.{table} holds {count} rows; not downgrading over data"

    from alembic import command
    from alembic.config import Config

    import research_engine

    ini = (
        Path(research_engine.__file__).parent
        / "adapters/storage/postgres/migrations/alembic.ini"
    )
    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent))
    previous = os.environ.get("RE_DB_URL")
    os.environ["RE_DB_URL"] = db_url
    try:

        await asyncio.to_thread(command.downgrade, config, "008_passage_node")
        async with engine.connect() as conn:
            schemas = (
                await conn.execute(
                    sa.text(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name IN ('evidence', 'argument')"
                    )
                )
            ).all()
        assert schemas == []
        await asyncio.to_thread(command.upgrade, config, "head")
    finally:
        if previous is None:
            os.environ.pop("RE_DB_URL", None)
        else:
            os.environ["RE_DB_URL"] = previous
    async with engine.connect() as conn:
        tables = (
            await conn.execute(
                sa.text(
                    "SELECT table_schema, table_name "
                    "FROM information_schema.tables "
                    "WHERE table_schema IN ('evidence', 'argument') "
                    "ORDER BY table_schema, table_name"
                )
            )
        ).all()
    assert tables == [
        ("argument", "anchors"),
        ("argument", "claim_edges"),
        ("argument", "claims"),
        ("evidence", "source_spans"),
    ]
