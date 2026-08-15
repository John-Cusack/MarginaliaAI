"""Corpus invariant checks, each proved against a planted defect.

Every check here is exercised by writing the broken row it is meant to find.
A diagnostic that has never been shown to fail is the same trap as a regression
test that has never been shown to fail — it reports "ok" either way, and the
"ok" is what you act on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.document_texts import (
    PGDocumentTextRepo,
)
from research_engine.services.diagnostics.corpus_check import (
    OVERSIZED_TOKENS,
    CorpusChecker,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

TEXT = "The archive holds letters. Each letter carries a date. Others are exact."


async def _check(engine: AsyncEngine, name: str):
    report = await CorpusChecker(engine).run()
    return next(c for c in report.checks if c.name == name)


async def _document_with_text(engine: AsyncEngine, corpus: Corpus, text: str = TEXT):
    doc_id = await corpus.add_document()
    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(tx, doc_id, text, "test", "1.0")
    return doc_id


async def test_a_healthy_passage_trips_nothing(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _document_with_text(engine, corpus)
    await corpus.add_passage(doc_id, TEXT[0:26], char_start=0, char_end=26)

    report = await CorpusChecker(engine).run()
    critical = [c.name for c in report.critical]

    assert critical == [], f"healthy corpus reported critical failures: {critical}"


async def test_text_that_disagrees_with_its_span_is_caught(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """The load-bearing invariant: a citation that will not verify."""
    doc_id = await _document_with_text(engine, corpus)
    # Span says one thing, stored text says another — exactly what a chunker
    # that alters text after computing offsets produces.
    pid = await corpus.add_passage(
        doc_id, "Text that is not what lives at these offsets.", char_start=0, char_end=26
    )

    check = await _check(engine, "passage_text_matches_its_span")

    assert check.count >= 1
    assert str(pid) in check.samples
    assert check.severity == "critical"
    assert "reindex chunks" in check.remedy


async def test_a_span_past_the_end_of_the_text_is_caught(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _document_with_text(engine, corpus)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO core.passages (id, document_id, position, char_start, "
                "char_end, locator, text, chunker, chunker_version, metadata, "
                "content_hash) VALUES (gen_random_uuid(), :d, 0, 0, 99999, '{}', "
                "'x', 'test', '1.0', '{}', '\\x00'::bytea)"
            ),
            {"d": doc_id},
        )

    assert (await _check(engine, "passage_span_within_the_text")).count >= 1


async def test_a_passage_with_no_offsets_is_caught(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """The pre-offsets era: present in the corpus, uncitable."""
    doc_id = await _document_with_text(engine, corpus)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO core.passages (id, document_id, position, locator, "
                "text, chunker, chunker_version, metadata, content_hash) VALUES "
                "(gen_random_uuid(), :d, 0, '{}', 'no offsets', 'test', '1.0', "
                "'{}', '\\x00'::bytea)"
            ),
            {"d": doc_id},
        )

    check = await _check(engine, "passage_has_offsets")
    assert check.count >= 1
    assert check.severity == "warning"


async def test_a_file_backed_document_without_text_is_routed_to_reparse(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await corpus.add_document(source="/books/somewhere.epub")
    await corpus.add_passage(doc_id, "Orphaned prose.")

    check = await _check(engine, "document_has_canonical_text")

    assert str(doc_id) in check.samples
    assert "reindex text" in check.remedy


async def test_a_pack_sourced_document_is_not_routed_to_a_command_that_cannot_help(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """`reindex text` classifies a pack URI as unreachable and skips it.

    Naming it as the remedy anyway is worse than naming none: the command
    reports success having done nothing, and the corpus looks repaired.
    """
    doc_id = await corpus.add_document(source="logos:LLS:46.30.12:batch:b0010")
    await corpus.add_passage(doc_id, "Prose fetched from an API.")

    reparse = await _check(engine, "document_has_canonical_text")
    pack = await _check(engine, "document_text_needs_a_pack_reingest")

    assert str(doc_id) not in reparse.samples
    assert str(doc_id) in pack.samples
    assert "plugin" in pack.remedy


async def test_an_oversized_passage_is_caught(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """The defect this tool was built after: a section emitted whole."""
    big = "The clerk recorded the transaction. " * (OVERSIZED_TOKENS // 2)
    doc_id = await _document_with_text(engine, corpus, big)
    pid = await corpus.add_passage(doc_id, big, char_start=0, char_end=len(big))

    check = await _check(engine, "passage_is_not_oversized")

    assert str(pid) in check.samples


async def test_an_unembedded_passage_is_caught(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _document_with_text(engine, corpus)
    pid = await corpus.add_passage(doc_id, TEXT[0:26], char_start=0, char_end=26)

    check = await _check(engine, "passage_is_embedded")

    assert str(pid) in check.samples
    assert "embeddings backfill" in check.remedy


async def test_a_node_outside_its_parent_is_caught(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """Containment is what subtree queries rest on."""
    from research_engine.adapters.storage.postgres.repositories.nodes import (
        PGDocumentNodeRepo,
    )
    from research_engine.domain.nodes import build_node_tree

    doc_id = await _document_with_text(engine, corpus)
    async with transaction(engine) as tx:
        stored = await PGDocumentNodeRepo(engine).insert_many(
            tx,
            doc_id,
            build_node_tree(
                [{"char_start": 0, "char_end": 40, "heading": "One", "level": 1}],
                text_length=len(TEXT),
            ),
        )
    child = next(n for n in stored if n.parent_id is not None)

    # Push the child outside its parent, as a bad widening pass would.
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE core.document_nodes SET char_end = 100000 WHERE id = :i"),
            {"i": child.id},
        )

    check = await _check(engine, "node_sits_inside_its_parent")
    assert str(child.id) in check.samples
    assert check.severity == "critical"


async def test_a_check_that_cannot_run_is_not_reported_as_passing(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """A failed query must degrade to 'info', never to a silent ok."""
    from research_engine.services.diagnostics import corpus_check

    broken = list(corpus_check._CHECKS)
    broken.append(
        ("deliberately_broken", "critical", "A check against a table that is not there",
         None, "SELECT id::text FROM core.no_such_table")
    )
    original = corpus_check._CHECKS
    corpus_check._CHECKS = broken
    try:
        check = await _check(engine, "deliberately_broken")
    finally:
        corpus_check._CHECKS = original

    assert check.severity == "info"
    assert "could not run" in check.description


async def test_one_broken_check_does_not_poison_the_rest(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """Postgres aborts a transaction on a failed statement.

    Without a rollback between checks, a single missing table — an unapplied
    migration is the obvious case — takes every later check down with it, and
    the report comes back looking mostly clean.
    """
    from research_engine.services.diagnostics import corpus_check

    doc_id = await _document_with_text(engine, corpus)
    await corpus.add_passage(doc_id, TEXT[0:26], char_start=0, char_end=26)

    original = corpus_check._CHECKS
    corpus_check._CHECKS = [
        ("broken_first", "critical", "Runs before everything else", None,
         "SELECT id::text FROM core.no_such_table"),
        *original,
    ]
    try:
        report = await CorpusChecker(engine).run()
    finally:
        corpus_check._CHECKS = original

    ran = [c for c in report.checks if "could not run" not in c.description]
    assert len(ran) >= len(original), (
        f"only {len(ran)} of {len(original)} checks survived the broken one"
    )
    # And the version-drift check, which runs last of all, still reported.
    assert any(c.name == "passages_on_current_chunker_versions" for c in report.checks)
