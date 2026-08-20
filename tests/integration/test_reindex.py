"""Re-chunk survival.

Appendix B calls this load-bearing: absent it, the re-chunk is a data-loss
event. `mentions`, `extractions` and `extraction_records` cascade on
`passages.id`; `events.source_passage_id` and `edges.source_passage_id` go to
NULL. Every one of them must come through pointing at the right new passage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import pytest
import sqlalchemy as sa
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.document_texts import (
    PGDocumentTextRepo,
)
from research_engine.adapters.storage.postgres.repositories.passages import PGPassageRepo
from research_engine.adapters.storage.postgres.schema import (
    edges,
    entities,
    events,
    extraction_records,
    extraction_schemas,
    extractions,
    mentions,
    passages,
)
from research_engine.domain.passages import PassageDraft
from research_engine.services.ingestion.reindex import ReindexService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]


def _id() -> UUID:
    return UUID(str(uuid7()))


DOC_TEXT = (
    "The archive holds letters from the campaign. Each letter carries a date. "
    "Some dates are approximate, written from memory. Others are exact, "
    "recorded by the clerk who filed them. A final note closes the folder."
)


async def _setup_stale_document(engine: AsyncEngine, corpus: Corpus) -> dict:
    """A document chunked by the *old* prose_window, with dependents attached."""
    text_repo = PGDocumentTextRepo(engine)
    passage_repo = PGPassageRepo(engine)
    doc_id = await corpus.add_document(language="en")

    # Two old-style passages: text with whitespace collapsed, as prose_window 1.0
    # emitted, and a span that does not describe them (byte_start was always 0).
    halves = [DOC_TEXT[:140], DOC_TEXT[140:]]
    old_drafts = [
        PassageDraft(
            position=i,
            char_start=0,
            char_end=len(" ".join(half.split())),
            text=" ".join(half.split()),
            chunker="prose_window",
            chunker_version="1.0",
            token_count=10,
        )
        for i, half in enumerate(halves)
    ]

    async with transaction(engine) as tx:
        await text_repo.put(tx, doc_id, DOC_TEXT, "test", "1.0")
        old_passages = await passage_repo.insert_many(tx, doc_id, old_drafts)

    entity_id = corpus.track(entities, _id())
    schema_id = corpus.track(extraction_schemas, _id())
    extraction_id = _id()
    # Neither of these cascades with the document: `events.source_passage_id` is
    # ON DELETE SET NULL, and edges address entities by (kind, id) with no
    # foreign key at all. Untracked, this fixture left one of each behind on
    # every run — 165 events and 165 edges accumulated in the live corpus.
    # Tracked after the entity so cleanup removes the edge before what it names.
    event_id = corpus.track(events, _id())
    edge_id = corpus.track(edges, _id())

    async with engine.begin() as conn:
        await conn.execute(
            entities.insert().values(
                id=entity_id, entity_type="person", canonical_name="The Clerk"
            )
        )
        await conn.execute(
            extraction_schemas.insert().values(
                # Unique per test: (name, version, owner) is unique corpus-wide.
                id=schema_id, name=f"claims-{schema_id}", version=1, owner="test",
                schema={}, prompt_template="",
            )
        )
        await conn.execute(
            mentions.insert().values(
                id=_id(), passage_id=old_passages[0].id, entity_id=entity_id,
                surface_form="the clerk", confidence=0.9, source="test",
            )
        )
        await conn.execute(
            extractions.insert().values(
                id=extraction_id, passage_id=old_passages[1].id, schema_id=schema_id,
                extractor_version="1.0", llm_model="test", status="ok", records=[],
            )
        )
        await conn.execute(
            extraction_records.insert().values(
                id=_id(), extraction_id=extraction_id, passage_id=old_passages[1].id,
                schema_id=schema_id, record_type="claim", data={"text": "a claim"},
            )
        )
        await conn.execute(
            events.insert().values(
                id=event_id, event_type="correspondence",
                source_passage_id=old_passages[0].id, payload={}, confidence=1.0,
            )
        )
        await conn.execute(
            edges.insert().values(
                id=edge_id, source_kind="entity", source_id=entity_id,
                target_kind="entity", target_id=entity_id, relation_type="same_work",
                source_passage_id=old_passages[1].id, attributes={}, confidence=1.0,
            )
        )

    return {
        "doc_id": doc_id,
        "old_passages": old_passages,
        "entity_id": entity_id,
        "schema_id": schema_id,
        "extraction_id": extraction_id,
        "event_id": event_id,
        "edge_id": edge_id,
    }


class FakeEmbedding:
    """Deterministic stand-in for bge-m3.

    The dimension must match the real model: `passage_embeddings.embedding` is
    `vector(1024)` since migration 006, so a stub with a convenient small
    dimension no longer inserts. That is the constraint doing its job — an
    8-dimensional vector was never usable by search anyway.
    """

    model_name = "fake-embedding"
    model_version = "1.0"
    dim = 1024

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t) % 7)] * self.dim for t in texts]

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]


def _service(engine: AsyncEngine, orphan_threshold: float = 0.005) -> ReindexService:
    return ReindexService(
        engine,
        PGPassageRepo(engine),
        PGDocumentTextRepo(engine),
        FakeEmbedding(),
        orphan_threshold=orphan_threshold,
    )


async def _passage_of(engine: AsyncEngine, table, column: str, row_id: UUID) -> UUID | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.select(table.c[column]).where(table.c.id == row_id)
            )
        ).scalar_one_or_none()


async def test_every_dependent_survives_and_is_repointed(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    setup = await _setup_stale_document(engine, corpus)
    old_ids = {p.id for p in setup["old_passages"]}

    report = await _service(engine).reindex_chunks([setup["doc_id"]])

    assert not report.aborted
    assert report.orphans == []
    assert report.documents_reindexed == 1

    passage_repo = PGPassageRepo(engine)
    new_passages = await passage_repo.get_by_document(setup["doc_id"])
    new_ids = {p.id for p in new_passages}
    assert new_ids.isdisjoint(old_ids), "old passages should be gone"
    assert new_passages, "new passages should exist"
    # Asked of the registry, not hardcoded: a version literal here means every
    # chunker bump breaks a test that has nothing to do with the change.
    from research_engine.services.ingestion.pipeline import current_chunker_versions

    expected = current_chunker_versions()
    assert all(
        p.chunker_version == expected[p.chunker] for p in new_passages
    ), f"expected {expected}, got {sorted({(p.chunker, p.chunker_version) for p in new_passages})}"

    # Nothing cascaded away, nothing nulled — and each points at a real new row.
    async with engine.connect() as conn:
        mention_pid = (
            await conn.execute(
                sa.select(mentions.c.passage_id).where(mentions.c.entity_id == setup["entity_id"])
            )
        ).scalar_one()
    assert mention_pid in new_ids

    assert await _passage_of(engine, extractions, "passage_id", setup["extraction_id"]) in new_ids
    assert await _passage_of(engine, events, "source_passage_id", setup["event_id"]) in new_ids
    assert await _passage_of(engine, edges, "source_passage_id", setup["edge_id"]) in new_ids

    async with engine.connect() as conn:
        record_pid = (
            await conn.execute(
                sa.select(extraction_records.c.passage_id).where(
                    extraction_records.c.extraction_id == setup["extraction_id"]
                )
            )
        ).scalar_one()
    assert record_pid in new_ids


async def test_new_passages_have_true_offsets(engine: AsyncEngine, corpus: Corpus) -> None:
    setup = await _setup_stale_document(engine, corpus)
    await _service(engine).reindex_chunks([setup["doc_id"]])

    canonical = await PGDocumentTextRepo(engine).get_text(setup["doc_id"])
    for passage in await PGPassageRepo(engine).get_by_document(setup["doc_id"]):
        assert passage.char_start is not None
        assert canonical[passage.char_start : passage.char_end] == passage.text


async def test_dry_run_writes_nothing(engine: AsyncEngine, corpus: Corpus) -> None:
    setup = await _setup_stale_document(engine, corpus)
    old_ids = {p.id for p in setup["old_passages"]}

    report = await _service(engine).reindex_chunks([setup["doc_id"]], dry_run=True)

    assert report.dry_run
    assert report.documents_reindexed == 1  # it would have re-chunked
    surviving = {p.id for p in await PGPassageRepo(engine).get_by_document(setup["doc_id"])}
    assert surviving == old_ids, "dry run must leave the corpus untouched"


async def test_dry_run_reports_what_would_be_repointed(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    setup = await _setup_stale_document(engine, corpus)
    report = await _service(engine).reindex_chunks([setup["doc_id"]], dry_run=True)
    assert report.repointed.get("mentions") == 1
    assert report.repointed.get("extractions") == 1
    assert report.repointed.get("extraction_records") == 1
    assert report.repointed.get("events") == 1
    assert report.repointed.get("edges") == 1


async def test_unmatchable_passage_is_reported_not_dropped(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """An orphan must surface with its dependent counts, not vanish quietly."""
    text_repo = PGDocumentTextRepo(engine)
    passage_repo = PGPassageRepo(engine)
    doc_id = await corpus.add_document()

    good = PassageDraft(
        position=0, char_start=0, char_end=len(DOC_TEXT), text=DOC_TEXT,
        chunker="prose_window", chunker_version="1.0", token_count=10,
    )
    # The unmatchable row carries a 1.0-style bogus span, which PassageDraft now
    # refuses to construct — so it goes in through raw SQL, exactly as a row
    # written before this validation existed would look.
    async with transaction(engine) as tx:
        await text_repo.put(tx, doc_id, DOC_TEXT, "test", "1.0")
        await passage_repo.insert_many(tx, doc_id, [good])
        await tx.conn.execute(
            passages.insert().values(
                id=_id(), document_id=doc_id, position=1, char_start=0, char_end=0,
                locator={}, text="Text that appears nowhere in the document.",
                token_count=10, chunker="prose_window", chunker_version="1.0",
                metadata={}, content_hash=b"x" * 32,
            )
        )

    report = await _service(engine).reindex_chunks([doc_id], dry_run=True)

    assert len(report.orphans) == 1
    assert "not found" in report.orphans[0].reason


async def test_orphan_rate_over_threshold_aborts_before_writing(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """The guarantee that makes this a migration rather than a loss event."""
    text_repo = PGDocumentTextRepo(engine)
    doc_id = await corpus.add_document()

    async with transaction(engine) as tx:
        await text_repo.put(tx, doc_id, DOC_TEXT, "test", "1.0")
        for i in range(4):
            await tx.conn.execute(
                passages.insert().values(
                    id=_id(), document_id=doc_id, position=i, char_start=0, char_end=0,
                    locator={}, text=f"Unmatchable passage number {i}.",
                    token_count=5, chunker="prose_window", chunker_version="1.0",
                    metadata={}, content_hash=bytes([i]) * 32,
                )
            )

    report = await _service(engine, orphan_threshold=0.005).reindex_chunks([doc_id])

    assert report.aborted
    assert report.orphan_rate > 0.005
    surviving = await PGPassageRepo(engine).get_by_document(doc_id)
    assert len(surviving) == 4
    assert all(p.chunker_version == "1.0" for p in surviving)


async def test_documents_without_canonical_text_are_skipped_not_wrecked(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    passage_repo = PGPassageRepo(engine)
    doc_id = await corpus.add_document()
    async with transaction(engine) as tx:
        await passage_repo.insert_many(
            tx, doc_id,
            [PassageDraft(
                position=0, char_start=0, char_end=len(DOC_TEXT), text=DOC_TEXT,
                chunker="prose_window", chunker_version="1.0", token_count=10,
            )],
        )

    report = await _service(engine).reindex_chunks([doc_id])

    assert doc_id in report.documents_without_text
    assert len(await passage_repo.get_by_document(doc_id)) == 1


async def test_already_current_documents_are_left_alone(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    text_repo = PGDocumentTextRepo(engine)
    passage_repo = PGPassageRepo(engine)
    doc_id = await corpus.add_document()

    from research_engine.services.ingestion.chunking.prose_window import ProseWindowChunker

    drafts = await ProseWindowChunker().chunk(DOC_TEXT)
    async with transaction(engine) as tx:
        await text_repo.put(tx, doc_id, DOC_TEXT, "test", "1.0")
        before = await passage_repo.insert_many(tx, doc_id, drafts)

    report = await _service(engine).reindex_chunks([doc_id])

    assert report.documents_up_to_date == 1
    assert report.documents_reindexed == 0
    after = await passage_repo.get_by_document(doc_id)
    assert {p.id for p in after} == {p.id for p in before}


async def test_new_passages_are_searchable_after_reindex(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """Re-chunking must not leave the document unsearchable.

    `passage_embeddings` and `passage_fts` cascade away with the old passages.
    If the new ones are not embedded and indexed, the re-chunk silently removes
    the document from both vector and keyword search.
    """
    setup = await _setup_stale_document(engine, corpus)
    await _service(engine).reindex_chunks([setup["doc_id"]])

    new_ids = [p.id for p in await PGPassageRepo(engine).get_by_document(setup["doc_id"])]
    assert new_ids

    async with engine.connect() as conn:
        embedded = (
            await conn.execute(
                sa.text(
                    "SELECT count(DISTINCT passage_id) FROM core.passage_embeddings "
                    "WHERE passage_id = ANY(:ids)"
                ),
                {"ids": new_ids},
            )
        ).scalar_one()
        indexed = (
            await conn.execute(
                sa.text(
                    "SELECT count(*) FROM core.passage_fts WHERE passage_id = ANY(:ids)"
                ),
                {"ids": new_ids},
            )
        ).scalar_one()

    assert embedded == len(new_ids), "new passages have no embeddings"
    assert indexed == len(new_ids), "new passages are not in the FTS index"


async def test_re_chunking_rebuilds_the_structure_tree(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """Structure was written at ingest and nowhere else.

    A corpus re-chunked since `document_nodes` existed therefore had a tree for
    no document at all, and `get_document_outline`, `read_node` and
    `locate_passage` returned nothing for every book in it. The section table
    the parser produced is long gone by re-chunk time, so the headings are read
    back out of the canonical text they are offsets into.
    """
    from research_engine.adapters.storage.postgres.repositories.nodes import (
        PGDocumentNodeRepo,
    )

    text = (
        "# The Peninsula\n\n"
        "The clerk recorded the transaction in the ledger. A second hand "
        "annotated the margin some years later.\n\n"
        "## Spring\n\n"
        "Letters slowed through April. The cadence resumes in May.\n\n"
        "## Summer\n\n"
        "Correspondence thickens again once the roads dry.\n"
    )
    doc_id = await corpus.add_document()
    old = PassageDraft(
        position=0, char_start=0, char_end=40, text=text[0:40],
        chunker="prose_window", chunker_version="1.0", token_count=10,
    )
    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(tx, doc_id, text, "test", "1.0")
        await PGPassageRepo(engine).insert_many(tx, doc_id, [old])

    service = ReindexService(
        engine,
        PGPassageRepo(engine),
        PGDocumentTextRepo(engine),
        FakeEmbedding(),
        document_node_repo=PGDocumentNodeRepo(engine),
    )
    report = await service.reindex_chunks([doc_id])

    assert report.nodes_written > 0, "re-chunk wrote no structure at all"

    nodes = await PGDocumentNodeRepo(engine).get_tree(doc_id)
    headings = [n.title for n in nodes if n.title]
    assert "The Peninsula" in headings
    assert "Spring" in headings and "Summer" in headings

    # Every node's span must address the text it claims, or the outline points
    # at prose that is not there.
    for node in nodes:
        assert 0 <= node.char_start <= node.char_end <= len(text)

    async with engine.connect() as conn:
        attached = (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(passages)
                .where(passages.c.document_id == doc_id, passages.c.node_id.isnot(None))
            )
        ).scalar_one()
    assert attached > 0, "passages were written without an owning node"


async def test_re_chunking_a_document_with_no_headings_still_gives_it_a_root(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """A lexicon has no headings, and must still be uniform with everything else.

    Returning no tree at all would make `locate_passage` answer "no structure"
    for a document that simply has one level of it.
    """
    from research_engine.adapters.storage.postgres.repositories.nodes import (
        PGDocumentNodeRepo,
    )

    text = "Bato Comicus iii b.c. Ed. T. Kock. Berosus Historicus iv b.c. Ed. Mueller. "
    doc_id = await corpus.add_document()
    old = PassageDraft(
        position=0, char_start=0, char_end=30, text=text[0:30],
        chunker="prose_window", chunker_version="1.0", token_count=10,
    )
    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(tx, doc_id, text, "test", "1.0")
        await PGPassageRepo(engine).insert_many(tx, doc_id, [old])

    service = ReindexService(
        engine,
        PGPassageRepo(engine),
        PGDocumentTextRepo(engine),
        FakeEmbedding(),
        document_node_repo=PGDocumentNodeRepo(engine),
    )
    await service.reindex_chunks([doc_id])

    nodes = await PGDocumentNodeRepo(engine).get_tree(doc_id)
    assert len(nodes) >= 1
    root = nodes[0]
    assert root.char_start == 0 and root.char_end == len(text), (
        "the root must cover the whole text, or subtree queries lose the tail"
    )
