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
async def test_migrations_revert_cleanly(db_url: str) -> None:
    """Every migration downgrades away entirely, then comes back.

    Against an isolated scratch database, never the dev corpus: the
    downgrade drops tables, and the corpus holds real rows (ingested
    editions) that must not be destroyed to test a migration. The scratch
    database is dropped afterwards, so no state leaks between runs.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import create_async_engine

    import research_engine

    scratch = (
        sa.engine.make_url(db_url)
        .set(database="research_engine_migrations")
        .render_as_string(hide_password=False)
    )
    admin_url = (
        sa.engine.make_url(db_url)
        .set(database="postgres")
        .render_as_string(hide_password=False)
    )
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        try:
            async with admin.connect() as conn:
                await conn.execute(
                    sa.text('DROP DATABASE IF EXISTS "research_engine_migrations"')
                )
                await conn.execute(
                    sa.text('CREATE DATABASE "research_engine_migrations"')
                )
        except Exception as exc:
            pytest.skip(f"Cannot provision a scratch database: {exc}")
        engine = create_async_engine(scratch)
        try:
            ini = (
                Path(research_engine.__file__).parent
                / "adapters/storage/postgres/migrations/alembic.ini"
            )
            config = Config(str(ini))
            config.set_main_option("script_location", str(ini.parent))
            previous = os.environ.get("RE_DB_URL")
            os.environ["RE_DB_URL"] = scratch
            try:
                await asyncio.to_thread(
                    command.upgrade, config, "017_vector_index_restore"
                )
                document_id = "11111111-1111-1111-1111-111111111111"
                span_id = "22222222-2222-2222-2222-222222222222"
                claim_id = "33333333-3333-3333-3333-333333333333"
                anchor_id = "44444444-4444-4444-4444-444444444444"
                edition_id = "55555555-5555-5555-5555-555555555555"
                edition_key = "MIGRATION-018-EDITION"
                identity_document_id = "66666666-6666-6666-6666-666666666666"
                identity_key = "MIGRATION-020-EDITION"
                plugin_ids = [
                    "logos",
                    "academic-journal",
                    "kindle",
                    "yourcloudlibrary",
                    "history",
                ]
                legacy_rows = [
                    {
                        "id": plugin_id,
                        "version": f"0.{index}.0",
                        "source_url": f"https://example.test/{plugin_id}.git",
                        "source_ref": f"commit-{index}",
                        "manifest": {
                            "name": plugin_id,
                            "provides": {"mcp_tools": [{"id": f"{plugin_id}.tool"}]},
                        },
                        "permissions": {
                            "network": "none",
                            "llm": plugin_id == "history",
                        },
                    }
                    for index, plugin_id in enumerate(plugin_ids, start=1)
                ]
                async with engine.begin() as conn:
                    await conn.execute(
                        sa.text(
                            "INSERT INTO core.documents "
                            "(id, document_type, source, content_hash, parser, parser_version) "
                            "VALUES (:id, 'book', 'migration-fixture', "
                            "decode(repeat('00', 32), 'hex'), 'test', '1')"
                        ),
                        {"id": document_id},
                    )
                    await conn.execute(
                        sa.text(
                            "INSERT INTO core.documents "
                            "(id, document_type, source, content_hash, parser, "
                            "parser_version, metadata) "
                            "VALUES (:id, 'generic', 'identity-fixture', "
                            "decode(repeat('01', 32), 'hex'), 'test', '1', "
                            "CAST(:metadata AS json))"
                        ),
                        {
                            "id": identity_document_id,
                            "metadata": json.dumps({"edition_key": identity_key}),
                        },
                    )
                    await conn.execute(
                        sa.text(
                            "INSERT INTO evidence.source_spans "
                            "(id, document_id, char_start, char_end, quoted_text) "
                            "VALUES (:id, :document_id, 0, 8, 'evidence')"
                        ),
                        {"id": span_id, "document_id": document_id},
                    )
                    await conn.execute(
                        sa.text(
                            "INSERT INTO argument.claims "
                            "(id, ref, statement, kind) "
                            "VALUES (:id, 'MIGRATION-018', 'Migration fixture.', 'premise')"
                        ),
                        {"id": claim_id},
                    )
                    await conn.execute(
                        sa.text(
                            "INSERT INTO bibliography.editions (id, edition_key) "
                            "VALUES (:id, :edition_key)"
                        ),
                        {"id": edition_id, "edition_key": edition_key},
                    )
                    await conn.execute(
                        sa.text(
                            "INSERT INTO argument.anchors "
                            "(id, claim_id, role, source_span_id, quoted_text, "
                            "verify_status, edition_key, edition) "
                            "VALUES (:id, :claim_id, 'supports', :span_id, "
                            "'evidence', 'exact', :edition_key, 'legacy text')"
                        ),
                        {
                            "id": anchor_id,
                            "claim_id": claim_id,
                            "span_id": span_id,
                            "edition_key": edition_key,
                        },
                    )
                    await conn.execute(
                        sa.text(
                            "INSERT INTO core.installed_packs "
                            "(id, version, source_url, source_ref, enabled, "
                            "manifest, permissions_granted) "
                            "VALUES (:id, :version, :source_url, :source_ref, true, "
                            "CAST(:manifest AS json), CAST(:permissions AS json))"
                        ),
                        [
                            {
                                **row,
                                "manifest": json.dumps(row["manifest"]),
                                "permissions": json.dumps(row["permissions"]),
                            }
                            for row in legacy_rows
                        ],
                    )

                async def anchor_schema() -> tuple[set[str], int, int]:
                    async with engine.connect() as conn:
                        columns = {
                            row[0]
                            for row in (
                                await conn.execute(
                                    sa.text(
                                        "SELECT column_name "
                                        "FROM information_schema.columns "
                                        "WHERE table_schema = 'argument' "
                                        "AND table_name = 'anchors'"
                                    )
                                )
                            ).all()
                        }
                        index_count = (
                            await conn.execute(
                                sa.text(
                                    "SELECT count(*) FROM pg_indexes "
                                    "WHERE schemaname = 'argument' "
                                    "AND indexname = 'anchors_edition_idx'"
                                )
                            )
                        ).scalar_one()
                        fk_count = (
                            await conn.execute(
                                sa.text(
                                    "SELECT count(*) FROM pg_constraint "
                                    "WHERE conname = 'anchors_edition_id_fk' "
                                    "AND conrelid = 'argument.anchors'::regclass"
                                )
                            )
                        ).scalar_one()
                    return columns, index_count, fk_count

                async def document_edition_schema() -> tuple[set[str], int, int]:
                    async with engine.connect() as conn:
                        columns = {
                            row[0]
                            for row in (
                                await conn.execute(
                                    sa.text(
                                        "SELECT column_name "
                                        "FROM information_schema.columns "
                                        "WHERE table_schema = 'core' "
                                        "AND table_name = 'documents'"
                                    )
                                )
                            ).all()
                        }
                        index_count = (
                            await conn.execute(
                                sa.text(
                                    "SELECT count(*) FROM pg_indexes "
                                    "WHERE schemaname = 'core' "
                                    "AND indexname = 'documents_edition_idx'"
                                )
                            )
                        ).scalar_one()
                        fk_count = (
                            await conn.execute(
                                sa.text(
                                    "SELECT count(*) FROM pg_constraint "
                                    "WHERE conname = 'documents_edition_id_fk' "
                                    "AND conrelid = 'core.documents'::regclass"
                                )
                            )
                        ).scalar_one()
                    return columns, index_count, fk_count

                async def assert_upgraded() -> None:
                    columns, index_count, fk_count = await anchor_schema()
                    assert "edition_id" in columns
                    assert "edition" not in columns
                    assert index_count == 1
                    assert fk_count == 1
                    async with engine.connect() as conn:
                        stored = (
                            await conn.execute(
                                sa.text(
                                    "SELECT edition_id FROM argument.anchors "
                                    "WHERE id = :id"
                                ),
                                {"id": anchor_id},
                            )
                        ).scalar_one()
                    assert str(stored) == edition_id
                    columns, index_count, fk_count = await document_edition_schema()
                    assert "edition_id" in columns
                    assert index_count == 1
                    assert fk_count == 1
                    async with engine.connect() as conn:
                        linked_key = (
                            await conn.execute(
                                sa.text(
                                    "SELECT e.edition_key "
                                    "FROM core.documents d "
                                    "JOIN bibliography.editions e ON e.id = d.edition_id "
                                    "WHERE d.id = :id"
                                ),
                                {"id": identity_document_id},
                            )
                        ).scalar_one()
                    assert linked_key == identity_key
                    async with engine.connect() as conn:
                        plugin_rows = (
                            await conn.execute(
                                sa.text(
                                    "SELECT plugin_id, distribution_version, "
                                    "legacy_source_url, legacy_source_ref, enabled, state, "
                                    "manifest, permissions_granted "
                                    "FROM core.plugin_activations ORDER BY plugin_id"
                                )
                            )
                        ).mappings().all()
                    assert len(plugin_rows) == 5
                    expected = {row["id"]: row for row in legacy_rows}
                    for row in plugin_rows:
                        original = expected[row["plugin_id"]]
                        assert row["distribution_version"] == original["version"]
                        assert row["legacy_source_url"] == original["source_url"]
                        assert row["legacy_source_ref"] == original["source_ref"]
                        assert row["enabled"] is False
                        assert row["state"] == "legacy"
                        assert row["manifest"] == original["manifest"]
                        assert row["permissions_granted"] == original["permissions"]

                async def assert_downgraded() -> None:
                    columns, index_count, fk_count = await anchor_schema()
                    assert "edition" in columns
                    assert "edition_id" not in columns
                    assert index_count == 0
                    assert fk_count == 0
                    document_columns, document_index, document_fk = (
                        await document_edition_schema()
                    )
                    assert "edition_id" not in document_columns
                    assert document_index == 0
                    assert document_fk == 0
                    async with engine.connect() as conn:
                        plugin_rows = (
                            await conn.execute(
                                sa.text(
                                    "SELECT id, version, source_url, source_ref, "
                                    "manifest, permissions_granted "
                                    "FROM core.installed_packs ORDER BY id"
                                )
                            )
                        ).mappings().all()
                    assert len(plugin_rows) == 5
                    expected = {row["id"]: row for row in legacy_rows}
                    for row in plugin_rows:
                        original = expected[row["id"]]
                        assert row["version"] == original["version"]
                        assert row["source_url"] == original["source_url"]
                        assert row["source_ref"] == original["source_ref"]
                        assert row["manifest"] == original["manifest"]
                        assert row["permissions_granted"] == original["permissions"]

                await asyncio.to_thread(command.upgrade, config, "head")
                await assert_upgraded()
                with pytest.raises(IntegrityError):
                    async with engine.begin() as conn:
                        await conn.execute(
                            sa.text(
                                "DELETE FROM bibliography.editions WHERE id = :id"
                            ),
                            {"id": edition_id},
                        )
                with pytest.raises(IntegrityError):
                    async with engine.begin() as conn:
                        await conn.execute(
                            sa.text(
                                "DELETE FROM bibliography.editions "
                                "WHERE edition_key = :edition_key"
                            ),
                            {"edition_key": identity_key},
                        )

                await asyncio.to_thread(
                    command.downgrade, config, "017_vector_index_restore"
                )
                await assert_downgraded()
                await asyncio.to_thread(command.upgrade, config, "head")
                await assert_upgraded()
                await asyncio.to_thread(
                    command.downgrade, config, "017_vector_index_restore"
                )
                await assert_downgraded()

                await asyncio.to_thread(command.downgrade, config, "008_passage_node")
                async with engine.connect() as conn:
                    schemas = (
                        await conn.execute(
                            sa.text(
                                "SELECT schema_name FROM information_schema.schemata "
                                "WHERE schema_name IN ('evidence', 'argument', "
                                "'authored', 'bibliography')"
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
                            "WHERE table_schema IN ('evidence', 'argument', "
                            "'authored', 'bibliography') "
                            "ORDER BY table_schema, table_name"
                        )
                    )
                ).all()
            assert tables == [
                ("argument", "anchors"),
                ("argument", "claim_edges"),
                ("argument", "claims"),
                ("authored", "block_entity_links"),
                ("authored", "block_source_links"),
                ("authored", "citation_items"),
                ("authored", "citation_occurrences"),
                ("authored", "waivers"),
                ("authored", "work_blocks"),
                ("authored", "work_revisions"),
                ("authored", "works"),
                ("bibliography", "editions"),
                ("evidence", "source_spans"),
            ]
        finally:
            await engine.dispose()
            async with admin.connect() as conn:
                await conn.execute(
                    sa.text('DROP DATABASE IF EXISTS "research_engine_migrations"')
                )
    finally:
        await admin.dispose()
