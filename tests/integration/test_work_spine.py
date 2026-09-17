"""The Phase-1 spine over the real database: rows, gates, freezes, loops.

Each test builds the services over real repositories and the fixture corpus
(a lexicon entry with known spans), then cleans up through the `corpus`
fixture: works first (cascading items and links), spans, documents, and any
tracked extras. Slugs are unique per test so a leaked work fails loudly
instead of aliasing another test's.
"""

from __future__ import annotations

import json
import uuid
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.exc import IntegrityError

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGCitationRepo,
    PGDocumentRepo,
    PGDocumentTextRepo,
    PGEditionRepo,
    PGEntityRepo,
    PGPassageRepo,
    PGSourceSpanRepo,
    PGWaiverRepo,
    PGWorkBlockRepo,
    PGWorkLinkRepo,
    PGWorkRepo,
    PGWorkRevisionRepo,
)
from research_engine.adapters.storage.postgres.schema import (
    editions,
    entities,
    work_revisions,
    works,
)
from research_engine.domain.citations import CitationItemDraft
from research_engine.domain.entities import EntityDraft
from research_engine.domain.errors import FrozenRevisionError, StaleWriteError
from research_engine.domain.passages import PassageDraft
from research_engine.domain.works import WorkBlockDraft
from research_engine.mcp.dispatch import dispatch_tool
from research_engine.services.ingestion.orchestrator import IngestionOrchestrator
from research_engine.services.verification import QuoteVerifier
from research_engine.services.works.assembly import assemble_revision, hash_assembled
from research_engine.services.works.attach import AttachRefused, CitationService
from research_engine.services.works.drafting import ImportRefused, WorkExportService
from research_engine.services.works.publication import (
    FreezeBlocked,
    WaiverGiven,
    WorkPublicationService,
)
from research_engine.services.works.trace import WorkTraceService
from research_engine.services.works.validate import WorkValidationService
from research_engine.services.works.verify import MAX_QUOTE_CHARS
from research_engine.services.works.work_service import WorkService
from research_engine.testing.corpus import new_id

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

_FIXTURES = Path(__file__).parent / "fixtures" / "works"
TEXT = (_FIXTURES / "lexicon_fixture.txt").read_text(encoding="utf-8")
SIDECAR = json.loads((_FIXTURES / "lexicon_fixture.json").read_text(encoding="utf-8"))
PROBES = SIDECAR["probes"]
EXACT = SIDECAR["spans"]["exact_sentence"]
REGION_TEXT = TEXT[0:130]


class _Spine:
    """Real services over real repositories, built per test."""

    def __init__(self, engine: AsyncEngine, **overrides: Any) -> None:
        tx_factory = partial(transaction, engine)
        self.works = WorkService(
            works=PGWorkRepo(engine),
            revisions=PGWorkRevisionRepo(engine),
            blocks=PGWorkBlockRepo(engine),
            citations=PGCitationRepo(engine),
            links=PGWorkLinkRepo(engine),
            spans=PGSourceSpanRepo(engine),
            transaction_factory=tx_factory,
        )
        verification = QuoteVerifier(
            PGDocumentTextRepo(engine), PGPassageRepo(engine), PGDocumentRepo(engine)
        )
        self.cite = CitationService(
            verification=verification,
            spans=PGSourceSpanRepo(engine),
            editions=PGEditionRepo(engine),
            citations=PGCitationRepo(engine),
            works=PGWorkRepo(engine),
            revisions=PGWorkRevisionRepo(engine),
            blocks=PGWorkBlockRepo(engine),
            passages=PGPassageRepo(engine),
            documents=PGDocumentRepo(engine),
            transaction_factory=tx_factory,
        )
        self.export = WorkExportService(
            works=PGWorkRepo(engine),
            revisions=PGWorkRevisionRepo(engine),
            blocks=PGWorkBlockRepo(engine),
            citations=PGCitationRepo(engine),
            links=PGWorkLinkRepo(engine),
            spans=PGSourceSpanRepo(engine),
            transaction_factory=tx_factory,
        )
        validation_kwargs: dict[str, Any] = {
            "works": PGWorkRepo(engine),
            "revisions": PGWorkRevisionRepo(engine),
            "blocks": PGWorkBlockRepo(engine),
            "citations": PGCitationRepo(engine),
            "links": PGWorkLinkRepo(engine),
            "editions": PGEditionRepo(engine),
            "waivers": PGWaiverRepo(engine),
            "spans": PGSourceSpanRepo(engine),
            "documents": PGDocumentRepo(engine),
            "document_texts": PGDocumentTextRepo(engine),
            "passages": PGPassageRepo(engine),
        }
        validation_kwargs.update(overrides)
        self.validate = WorkValidationService(**validation_kwargs)
        self.publish = WorkPublicationService(
            validation=self.validate,
            works=PGWorkRepo(engine),
            revisions=PGWorkRevisionRepo(engine),
            blocks=PGWorkBlockRepo(engine),
            citations=PGCitationRepo(engine),
            links=PGWorkLinkRepo(engine),
            spans=PGSourceSpanRepo(engine),
            waivers=PGWaiverRepo(engine),
            transaction_factory=tx_factory,
        )
        self.trace = WorkTraceService(
            works=PGWorkRepo(engine),
            revisions=PGWorkRevisionRepo(engine),
            blocks=PGWorkBlockRepo(engine),
            citations=PGCitationRepo(engine),
            links=PGWorkLinkRepo(engine),
            spans=PGSourceSpanRepo(engine),
            documents=PGDocumentRepo(engine),
        )
        self.repos = SimpleNamespace(
            works=PGWorkRepo(engine),
            revisions=PGWorkRevisionRepo(engine),
            blocks=PGWorkBlockRepo(engine),
            citations=PGCitationRepo(engine),
            editions=PGEditionRepo(engine),
            waivers=PGWaiverRepo(engine),
            spans=PGSourceSpanRepo(engine),
            texts=PGDocumentTextRepo(engine),
        )


async def _ingest(engine: AsyncEngine, corpus: Corpus, **metadata: str) -> UUID:
    doc_id = await corpus.add_document(title="Dabaris", metadata=dict(metadata) or None)
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

async def _ingest_quoted_text(
    engine: AsyncEngine,
    corpus: Corpus,
    *,
    title: str,
    edition_key: str,
    text: str,
) -> UUID:
    document_id = await corpus.add_document(
        title=title, metadata={"edition_key": edition_key}
    )
    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(
            tx, document_id, text, "test", "license-quota-1"
        )
        await PGPassageRepo(engine).insert_many(
            tx,
            document_id,
            [
                PassageDraft(
                    position=0,
                    char_start=0,
                    char_end=len(text),
                    text=text,
                    chunker="test",
                    chunker_version="1.0",
                )
            ],
        )
        edition = await PGEditionRepo(engine).upsert_key(tx, edition_key)
    corpus.track(editions, edition.id)
    return document_id


async def _work_with_cited_paragraph(
    engine: AsyncEngine, corpus: Corpus, slug: str, doc_id: UUID
) -> dict[str, Any]:
    """A work whose paragraph cites the exact probe and carries its marker."""
    spine = _Spine(engine)
    created = await spine.works.create(slug=slug, title=slug, work_type="essay")
    corpus.track(works, created.work_id)
    heading = await spine.works.upsert_block(
        slug=slug, position=0, block_type="heading", title="Release", body_markdown=""
    )
    paragraph = await spine.works.upsert_block(
        slug=slug, position=0, block_type="paragraph",
        body_markdown="The prophets speak.", parent_key=heading.block_key,
    )
    attached = await spine.cite.attach(
        slug=slug, block_key=paragraph.block_key, intent="quotation",
        quote=PROBES["exact"], document_id=doc_id, edition_key="DABAR_2026",
    )
    corpus.adopt_span(attached.item.source_span_id)
    paragraph = await spine.works.upsert_block(
        slug=slug, position=0, block_type="paragraph",
        body_markdown=f"The prophets speak. {attached.marker}",
        block_key=paragraph.block_key, parent_key=heading.block_key,
        expected_updated_at=paragraph.updated_at,
    )
    return {
        "spine": spine, "heading": heading, "paragraph": paragraph,
        "attached": attached,
    }


@pytest.mark.asyncio
async def test_create_get_roundtrip(engine: AsyncEngine, corpus: Corpus) -> None:
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-roundtrip", title="Roundtrip", work_type="essay"
    )
    corpus.track(works, created.work_id)

    assert created.revision_number == 1
    assert created.state == "draft"

    heading = await spine.works.upsert_block(
        slug="spine-roundtrip", position=0, block_type="heading",
        title="Release", body_markdown="",
    )
    paragraph = await spine.works.upsert_block(
        slug="spine-roundtrip", position=0, block_type="paragraph",
        body_markdown="Body.", parent_key=heading.block_key,
    )
    view = await spine.works.get(slug="spine-roundtrip")

    assert [block["block_type"] for block in view["blocks"]] == ["heading", "paragraph"]
    assert view["blocks"][1]["parent_key"] == str(heading.block_key)
    assert view["blocks"][1]["block_key"] == str(paragraph.block_key)
    assert view["blocks"][1]["citations"] == []


@pytest.mark.asyncio
async def test_upsert_conflict(engine: AsyncEngine, corpus: Corpus) -> None:
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-conflict", title="Conflict", work_type="essay"
    )
    corpus.track(works, created.work_id)
    block = await spine.works.upsert_block(
        slug="spine-conflict", position=0, block_type="paragraph", body_markdown="v1"
    )

    with pytest.raises(StaleWriteError):
        await spine.works.upsert_block(
            slug="spine-conflict", position=0, block_type="paragraph",
            body_markdown="v2", block_key=block.block_key,
            expected_updated_at="not-the-timestamp",
        )

    updated = await spine.works.upsert_block(
        slug="spine-conflict", position=0, block_type="paragraph",
        body_markdown="v2", block_key=block.block_key,
        expected_updated_at=block.updated_at,
    )
    assert updated.block_key == block.block_key


@pytest.mark.asyncio
async def test_revision_copy_preserves_keys(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    built = await _work_with_cited_paragraph(engine, corpus, "spine-copy", doc_id)
    spine = built["spine"]

    work = await spine.repos.works.get_by_slug("spine-copy")
    assert work is not None and work.current_revision_id is not None
    async with transaction(engine) as tx:
        copied = await spine.repos.revisions.copy_forward(tx, work.current_revision_id)

    assert copied.revision_number == 2
    old_tree = await spine.repos.blocks.tree(work.current_revision_id)
    new_tree = await spine.repos.blocks.tree(copied.id)
    assert [block.block_key for block in new_tree] == [
        block.block_key for block in old_tree
    ]
    assert {block.id for block in new_tree}.isdisjoint({block.id for block in old_tree})
    old_citations = await spine.repos.citations.for_revision(work.current_revision_id)
    new_citations = await spine.repos.citations.for_revision(copied.id)
    assert [
        entry.occurrence.citation_key for entry in new_citations
    ] == [entry.occurrence.citation_key for entry in old_citations]


@pytest.mark.asyncio
async def test_frozen_is_immutable(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    built = await _work_with_cited_paragraph(engine, corpus, "spine-frozen", doc_id)
    spine = built["spine"].works
    cite = built["spine"].cite
    paragraph = built["paragraph"]

    sealed = await built["spine"].publish.freeze(slug="spine-frozen", message="first")
    assert sealed.state == "frozen"

    with pytest.raises(FrozenRevisionError):
        await spine.upsert_block(
            slug="spine-frozen", position=1, block_type="paragraph", body_markdown="late"
        )
    with pytest.raises(FrozenRevisionError):
        await cite.attach(
            slug="spine-frozen", block_key=paragraph.block_key, intent="support",
            edition_key="DABAR_2026",
        )
    with pytest.raises(FrozenRevisionError):
        await spine.link(
            slug="spine-frozen", block_key=paragraph.block_key, relation="discusses",
            document_id=doc_id, char_start=EXACT["char_start"], char_end=EXACT["char_end"],
        )
    async with transaction(engine) as tx:
        with pytest.raises(FrozenRevisionError):
            await built["spine"].repos.citations.insert_item(
                tx,
                CitationItemDraft(
                    occurrence_id=built["attached"].occurrence_id, position=1,
                    edition_key="DABAR_2026",
                ),
            )

    report = await built["spine"].validate.validate(slug="spine-frozen", gate="freeze")
    assert report.gate.passed is True


@pytest.mark.asyncio
async def test_marker_bijection(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-markers", title="Markers", work_type="essay"
    )
    corpus.track(works, created.work_id)
    bare = await spine.works.upsert_block(
        slug="spine-markers", position=0, block_type="paragraph", body_markdown="No marker."
    )
    attached = await spine.cite.attach(
        slug="spine-markers", block_key=bare.block_key, intent="support",
        quote=PROBES["exact"], document_id=doc_id, edition_key="DABAR_2026",
    )
    corpus.adopt_span(attached.item.source_span_id)
    dangling = await spine.works.upsert_block(
        slug="spine-markers", position=1, block_type="paragraph",
        body_markdown="Points nowhere {{cite:99999999-9999-9999-9999-999999999999}}.",
    )

    report = await spine.validate.validate(slug="spine-markers", gate="none")
    rules = {
        (finding.rule_id, finding.citation_key, finding.block_key)
        for finding in report.findings
    }

    assert (
        "AUTH_CITATION_MARKER_MISSING", str(attached.citation_key), str(bare.block_key)
    ) in rules
    assert any(
        rule == "AUTH_CITATION_MARKER_DANGLING" and block == str(dangling.block_key)
        for rule, _cited, block in rules
    )


@pytest.mark.asyncio
async def test_cross_revision_parent_rejected(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-parent", title="Parent", work_type="essay"
    )
    corpus.track(works, created.work_id)
    root = await spine.works.upsert_block(
        slug="spine-parent", position=0, block_type="heading",
        title="Root", body_markdown="",
    )
    work = await spine.repos.works.get_by_slug("spine-parent")
    assert work is not None and work.current_revision_id is not None
    async with transaction(engine) as tx:
        copied = await spine.repos.revisions.copy_forward(tx, work.current_revision_id)

    with pytest.raises(IntegrityError):
        async with transaction(engine) as tx:
            await spine.repos.blocks.upsert(
                tx,
                copied.id,
                WorkBlockDraft(
                    revision_id=copied.id,
                    block_key=new_id(),
                    parent_id=root.block_id,  # a row of the other revision
                    position=0,
                    block_type="paragraph",
                    body_markdown="Stranger.",
                ),
                expected_updated_at=None,
            )


@pytest.mark.asyncio
async def test_work_cite_atomicity(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-atomic", title="Atomic", work_type="essay"
    )
    corpus.track(works, created.work_id)
    block = await spine.works.upsert_block(
        slug="spine-atomic", position=0, block_type="paragraph", body_markdown="Text."
    )

    async def _boom(tx: Any, draft: Any) -> Any:
        raise RuntimeError("induced failure after the span insert")

    original = spine.repos.citations.insert_occurrence
    spine.repos.citations.insert_occurrence = _boom  # type: ignore[method-assign]
    cite = CitationService(
        verification=spine.cite._verification,
        spans=spine.repos.spans,
        editions=spine.repos.editions,
        citations=spine.repos.citations,
        works=spine.repos.works,
        revisions=spine.repos.revisions,
        blocks=spine.repos.blocks,
        passages=PGPassageRepo(engine),
        documents=PGDocumentRepo(engine),
        transaction_factory=partial(transaction, engine),
    )
    try:
        with pytest.raises(RuntimeError, match="induced failure"):
            await cite.attach(
                slug="spine-atomic", block_key=block.block_key, intent="quotation",
                quote=PROBES["exact"], document_id=doc_id, edition_key="DABAR_2026",
            )
    finally:
        spine.repos.citations.insert_occurrence = original  # type: ignore[method-assign]

    assert await spine.repos.spans.for_document(doc_id) == []
    work = await spine.repos.works.get_by_slug("spine-atomic")
    assert work is not None and work.current_revision_id is not None
    assert await spine.repos.citations.for_revision(work.current_revision_id) == []


@pytest.mark.asyncio
async def test_identity_join(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-join", title="Join", work_type="essay"
    )
    corpus.track(works, created.work_id)
    first_block = await spine.works.upsert_block(
        slug="spine-join", position=0, block_type="paragraph", body_markdown="One."
    )
    second_block = await spine.works.upsert_block(
        slug="spine-join", position=1, block_type="paragraph", body_markdown="Two."
    )

    first = await spine.cite.attach(
        slug="spine-join", block_key=first_block.block_key, intent="quotation",
        quote=PROBES["exact"], document_id=doc_id, edition_key="DABAR_2026",
    )
    second = await spine.cite.attach(
        slug="spine-join", block_key=second_block.block_key, intent="support",
        quote=PROBES["exact"], document_id=doc_id, edition_key="DABAR_2026",
    )
    corpus.adopt_span(first.item.source_span_id)

    assert first.item.source_span_id == second.item.source_span_id
    assert first.citation_key != second.citation_key
    assert len(await spine.repos.spans.for_document(doc_id)) == 1


@pytest.mark.asyncio
async def test_tier_per_row(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-tier", title="Tier", work_type="essay"
    )
    corpus.track(works, created.work_id)
    clean_block = await spine.works.upsert_block(
        slug="spine-tier", position=0, block_type="paragraph", body_markdown="Clean."
    )
    noisy_block = await spine.works.upsert_block(
        slug="spine-tier", position=1, block_type="paragraph", body_markdown="Noisy."
    )

    clean = await spine.cite.attach(
        slug="spine-tier", block_key=clean_block.block_key, intent="quotation",
        quote=PROBES["exact"], document_id=doc_id, edition_key="DABAR_2026",
    )
    noisy = await spine.cite.attach(
        slug="spine-tier", block_key=noisy_block.block_key, intent="quotation",
        quote=PROBES["normalized_typed"], document_id=doc_id, edition_key="DABAR_2026",
    )
    corpus.adopt_span(clean.item.source_span_id)
    corpus.adopt_span(noisy.item.source_span_id)

    assert clean.item.verify_status == "exact"
    assert noisy.item.verify_status == "normalized"


@pytest.mark.asyncio
async def test_edition_inherited_from_document(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus, edition_key="DABAR_2026")
    async with transaction(engine) as tx:
        edition = await PGEditionRepo(engine).upsert_key(tx, "DABAR_2026")
    corpus.track(editions, edition.id)
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-inherit", title="Inherit", work_type="essay"
    )
    corpus.track(works, created.work_id)
    block = await spine.works.upsert_block(
        slug="spine-inherit", position=0, block_type="paragraph", body_markdown="Held."
    )

    attached = await spine.cite.attach(
        slug="spine-inherit", block_key=block.block_key, intent="quotation",
        quote=PROBES["exact"], document_id=doc_id,
    )
    corpus.adopt_span(attached.item.source_span_id)

    assert attached.item.edition_key == "DABAR_2026"
    assert attached.item.edition_id == edition.id


@pytest.mark.asyncio
async def test_edition_refused_without_any_source(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    keyless = await _ingest(engine, corpus)
    keyed = await _ingest(engine, corpus, edition_key="DABAR_2026")
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-noidentity", title="NoIdentity", work_type="essay"
    )
    corpus.track(works, created.work_id)
    bare = await spine.works.upsert_block(
        slug="spine-noidentity", position=0, block_type="paragraph", body_markdown="Bare."
    )
    keyless_block = await spine.works.upsert_block(
        slug="spine-noidentity", position=1, block_type="paragraph", body_markdown="Keyless."
    )

    # Spanless with no identity: a bibliography entry with no source.
    with pytest.raises(AttachRefused) as exc_info:
        await spine.cite.attach(
            slug="spine-noidentity", block_key=bare.block_key, intent="support"
        )
    assert exc_info.value.rule_id == "AUTH_CITATION_EDITION_MISSING"

    # A quote against a document nobody keyed: nothing to inherit.
    with pytest.raises(AttachRefused) as exc_info:
        await spine.cite.attach(
            slug="spine-noidentity", block_key=keyless_block.block_key,
            intent="quotation", quote=PROBES["exact"], document_id=keyless,
        )
    assert exc_info.value.rule_id == "AUTH_CITATION_EDITION_MISSING"

    assert await spine.repos.spans.for_document(keyless) == []
    assert await spine.repos.spans.for_document(keyed) == []


@pytest.mark.asyncio
async def test_explicit_identity_wins_and_mismatch_reported(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus, edition_key="DABAR_2026")
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-mismatch", title="Mismatch", work_type="essay"
    )
    corpus.track(works, created.work_id)
    block = await spine.works.upsert_block(
        slug="spine-mismatch", position=0, block_type="paragraph", body_markdown="Claimed."
    )
    attached = await spine.cite.attach(
        slug="spine-mismatch", block_key=block.block_key, intent="quotation",
        quote=PROBES["exact"], document_id=doc_id, edition_key="ESV",
    )
    corpus.adopt_span(attached.item.source_span_id)

    assert attached.item.edition_key == "ESV"
    await spine.works.upsert_block(
        slug="spine-mismatch", position=0, block_type="paragraph",
        body_markdown=f"Claimed. {attached.marker}",
        block_key=block.block_key, expected_updated_at=block.updated_at,
    )
    report = await spine.validate.validate(slug="spine-mismatch", gate="none")
    assert "AUTH_CITATION_EDITION_MISMATCH" in {
        finding.rule_id for finding in report.findings
        if finding.severity == "error"
    }


@pytest.mark.asyncio
async def test_narrowing(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-narrow", title="Narrow", work_type="essay"
    )
    corpus.track(works, created.work_id)
    strict = await spine.works.upsert_block(
        slug="spine-narrow", position=0, block_type="paragraph", body_markdown="Strict."
    )
    loose = await spine.works.upsert_block(
        slug="spine-narrow", position=1, block_type="paragraph", body_markdown="Loose."
    )
    warned = await spine.works.upsert_block(
        slug="spine-narrow", position=2, block_type="paragraph", body_markdown="Warned."
    )

    with pytest.raises(AttachRefused) as exc_info:
        await spine.cite.attach(
            slug="spine-narrow", block_key=strict.block_key, intent="quotation",
            quote=REGION_TEXT, document_id=doc_id, edition_key="DABAR_2026",
        )
    assert exc_info.value.rule_id == "AUTH_SPAN_NOT_NARROWED"

    background = await spine.cite.attach(
        slug="spine-narrow", block_key=loose.block_key, intent="background",
        quote=REGION_TEXT, document_id=doc_id, edition_key="DABAR_2026",
    )
    assert background.warnings == []
    corpus.adopt_span(background.item.source_span_id)

    support = await spine.cite.attach(
        slug="spine-narrow", block_key=warned.block_key, intent="support",
        quote=REGION_TEXT, document_id=doc_id, edition_key="DABAR_2026",
    )
    assert support.warnings == ["AUTH_SPAN_REGION"]
    corpus.adopt_span(support.item.source_span_id)


@pytest.mark.asyncio
async def test_window_hint_avoids_the_whole_document_search(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-window", title="Window", work_type="essay"
    )
    corpus.track(works, created.work_id)
    block = await spine.works.upsert_block(
        slug="spine-window", position=0, block_type="paragraph", body_markdown="Pinned."
    )

    calls = []
    original = PGDocumentTextRepo.find_raw

    async def _counting(self: Any, document_id: UUID, needle: str) -> Any:
        calls.append((document_id, needle))
        return await original(self, document_id, needle)

    PGDocumentTextRepo.find_raw = _counting  # type: ignore[method-assign]
    try:
        attached = await spine.cite.attach(
            slug="spine-window", block_key=block.block_key, intent="quotation",
            quote=PROBES["exact"], document_id=doc_id, window=(30, 70),
            edition_key="DABAR_2026",
        )
    finally:
        PGDocumentTextRepo.find_raw = original  # type: ignore[method-assign]

    assert calls == []
    assert [attached.item.char_start, attached.item.char_end] == [
        EXACT["char_start"], EXACT["char_end"],
    ]
    corpus.adopt_span(attached.item.source_span_id)


@pytest.mark.asyncio
async def test_editions(engine: AsyncEngine, corpus: Corpus) -> None:
    spine = _Spine(engine)

    class _Embedding:
        model_name = "test"
        model_version = "1"
        dim = 1024

        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 1024 for _ in texts]

    orchestrator = IngestionOrchestrator(
        docs=PGDocumentRepo(engine),
        passages=PGPassageRepo(engine),
        embedding=_Embedding(),  # type: ignore[arg-type]
        ingestion_runs=None,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        engine=engine,
        document_texts=PGDocumentTextRepo(engine),
        editions=PGEditionRepo(engine),
    )
    result = await orchestrator.ingest_drafts(
        "Keyed",
        "test_doc",
        [
            PassageDraft(
                position=0, char_start=0, char_end=len(TEXT), text=TEXT,
                chunker="test", chunker_version="1.0",
            )
        ],
        source="test://keyed",
        metadata={"edition_key": "DABAR_2026"},
        language="en",
        full_text=TEXT,
    )
    doc_id = uuid.UUID(result["document_id"])
    corpus.adopt(doc_id)

    edition = await spine.repos.editions.get_by_key("DABAR_2026")
    assert edition is not None
    corpus.track(editions, edition.id)

    created = await spine.works.create(
        slug="spine-editions", title="Editions", work_type="essay"
    )
    corpus.track(works, created.work_id)
    block = await spine.works.upsert_block(
        slug="spine-editions", position=0, block_type="paragraph", body_markdown="Keyed."
    )
    attached = await spine.cite.attach(
        slug="spine-editions", block_key=block.block_key, intent="support",
        quote=PROBES["exact"], document_id=doc_id, edition_key="DABAR_2026",
    )
    corpus.adopt_span(attached.item.source_span_id)

    assert attached.item.edition_id == edition.id


@pytest.mark.asyncio
async def test_freeze_waiver_and_stable_hash(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-freeze", title="Freeze", work_type="essay"
    )
    corpus.track(works, created.work_id)
    block = await spine.works.upsert_block(
        slug="spine-freeze", position=0, block_type="paragraph",
        body_markdown="Almost right.",
    )
    attached = await spine.cite.attach(
        slug="spine-freeze", block_key=block.block_key, intent="quotation",
        quote=PROBES["near"], document_id=doc_id, edition_key="DABAR_2026",
    )
    corpus.adopt_span(attached.item.source_span_id)
    assert attached.item.verify_status == "near"
    await spine.works.upsert_block(
        slug="spine-freeze", position=0, block_type="paragraph",
        body_markdown=f"Almost right. {attached.marker}",
        block_key=block.block_key, expected_updated_at=block.updated_at,
    )

    with pytest.raises(FreezeBlocked) as exc_info:
        await spine.publish.freeze(slug="spine-freeze", message="first")
    assert exc_info.value.blockers == ["AUTH_QUOTE_UNVERIFIED"]

    sealed = await spine.publish.freeze(
        slug="spine-freeze",
        message="first",
        waivers=[
            WaiverGiven(
                rule_id="AUTH_QUOTE_UNVERIFIED",
                subject=str(attached.citation_key),
                reason="OCR noise reviewed against the scan",
            )
        ],
    )
    assert sealed.state == "frozen"
    assert sealed.waivers == ["AUTH_QUOTE_UNVERIFIED"]

    stored = await spine.repos.waivers.for_revision(sealed.revision_id)
    assert [row.rule_id for row in stored] == ["AUTH_QUOTE_UNVERIFIED"]

    work = await spine.repos.works.get_by_slug("spine-freeze")
    assert work is not None and work.current_revision_id is not None
    revision = await spine.repos.revisions.get(work.current_revision_id)
    assert revision is not None and revision.content_hash is not None
    assert revision.content_hash.hex() == sealed.content_hash

    view = await assemble_revision(
        work,
        revision,
        blocks=spine.repos.blocks,
        citations=spine.repos.citations,
        links=PGWorkLinkRepo(engine),
        spans=spine.repos.spans,
    )
    assert hash_assembled(view) == revision.content_hash
    assert hash_assembled(view).hex() == sealed.content_hash


@pytest.mark.asyncio
async def test_drafting_loop(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    built = await _work_with_cited_paragraph(engine, corpus, "spine-loop", doc_id)
    spine = built["spine"]
    heading_key = str(built["heading"].block_key)
    paragraph_key = str(built["paragraph"].block_key)

    exported = await spine.export.export_draft(slug="spine-loop")
    edited = exported.replace(
        "The prophets speak.", "The prophets speak twice."
    ).replace(
        f"<!-- block:{built['paragraph'].block_key} -->\n"
        f"The prophets speak twice. {built['attached'].marker}",
        f"<!-- block:{built['paragraph'].block_key} -->\n"
        f"The prophets speak twice. {built['attached'].marker}\n\nA new claim.",
    )
    diff = await spine.export.import_draft(slug="spine-loop", markdown=edited)

    assert diff.revision_number == 2
    kinds = {change.block_key: change.change for change in diff.changes}
    assert kinds[paragraph_key] == "updated"
    assert sorted(kinds.values()) == ["added", "updated"]
    assert heading_key not in kinds

    view = await spine.works.get(slug="spine-loop")
    assert view["revision"]["revision_number"] == 2
    bodies = {block["block_key"]: block["body_markdown"] for block in view["blocks"]}
    assert "twice" in bodies[paragraph_key]
    assert len(view["blocks"]) == 3

    dry = await spine.export.import_draft(
        slug="spine-loop", markdown=await spine.export.export_draft(slug="spine-loop"),
        dry_run=True,
    )
    assert dry.dry_run is True
    assert dry.changes == []
    assert (await spine.works.get(slug="spine-loop"))["revision"]["revision_number"] == 2


@pytest.mark.asyncio
async def test_dangling_import_refuses(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    built = await _work_with_cited_paragraph(engine, corpus, "spine-dangle", doc_id)
    spine = built["spine"]

    exported = await spine.export.export_draft(slug="spine-dangle")
    forged = exported + "\nGhost {{cite:99999999-9999-9999-9999-999999999999}}.\n"
    with pytest.raises(ImportRefused) as exc_info:
        await spine.export.import_draft(slug="spine-dangle", markdown=forged)
    assert exc_info.value.rule_id == "AUTH_CITATION_MARKER_DANGLING"

    assert (await spine.works.get(slug="spine-dangle"))["revision"]["revision_number"] == 1


@pytest.mark.asyncio
async def test_flip_drift(engine: AsyncEngine, corpus: Corpus, tmp_path: Path) -> None:
    doc_id = await _ingest(engine, corpus)
    built = await _work_with_cited_paragraph(engine, corpus, "spine-drift", doc_id)
    spine = _Spine(
        engine, works_dir=tmp_path, export_markdown=built["spine"].export.export_draft_text
    )
    work = await spine.repos.works.get_by_slug("spine-drift")
    assert work is not None and work.current_revision_id is not None
    async with transaction(engine) as tx:
        await tx.conn.execute(
            work_revisions.update()
            .where(work_revisions.c.id == work.current_revision_id)
            .values(metadata={"port": {"file": "spine-drift.md"}})
        )
    rendered = await built["spine"].export.export_draft(slug="spine-drift")
    (tmp_path / "spine-drift.md").write_text(rendered, encoding="utf-8")

    clean = await spine.validate.validate(slug="spine-drift", gate="none")
    assert "AUTH_FILE_DRIFT" not in {finding.rule_id for finding in clean.findings}

    with (tmp_path / "spine-drift.md").open("a", encoding="utf-8") as handle:
        handle.write("\nA hand edit.\n")
    drifted = await spine.validate.validate(slug="spine-drift", gate="none")
    assert "AUTH_FILE_DRIFT" in {finding.rule_id for finding in drifted.findings}
    assert (await spine.works.get(slug="spine-drift"))["revision"]["revision_number"] == 1


@pytest.mark.asyncio
async def test_entity_link_query(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    built = await _work_with_cited_paragraph(engine, corpus, "spine-entity", doc_id)
    spine = built["spine"]
    async with transaction(engine) as tx:
        entity = await PGEntityRepo(engine).insert(
            tx, EntityDraft(entity_type="lemma", canonical_name="logos")
        )
    corpus.track(entities, entity.id)

    written = await spine.works.link(
        slug="spine-entity", block_key=built["paragraph"].block_key,
        relation="renders", entity_id=entity.id,
    )
    assert written.kind == "entity"

    found = await PGWorkLinkRepo(engine).for_entity(entity.id, "renders")
    assert [link.block_id for link in found] == [built["paragraph"].block_id]


@pytest.mark.asyncio
async def test_stale_spans(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    built = await _work_with_cited_paragraph(engine, corpus, "spine-stale", doc_id)
    spine = built["spine"]

    async with transaction(engine) as tx:
        await spine.repos.texts.put(tx, doc_id, TEXT, "test", "2.0")

    report = await spine.validate.validate(slug="spine-stale", gate="none")
    assert "AUTH_SOURCE_SPAN_STALE" in {
        finding.rule_id for finding in report.findings
        if finding.citation_key == str(built["attached"].citation_key)
    }


@pytest.mark.asyncio
async def test_trace(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    built = await _work_with_cited_paragraph(engine, corpus, "spine-trace", doc_id)
    spine = built["spine"]

    down = await spine.trace.trace(slug="spine-trace")
    assert down.kind == "work"
    assert down.children, "the tree must reach the blocks"

    span_id = str(built["attached"].item.source_span_id)
    up = await spine.trace.trace(source_span_id=span_id)
    assert up.kind == "source_span"
    assert any(
        child.kind == "citation"
        and child.id == str(built["attached"].citation_key)
        for child in up.children
    ), "the span must name its citing occurrence"

    document = await spine.trace.trace(document_id=str(doc_id))
    assert document.kind == "document"
    assert document.children, "the document must name its spans"


@pytest.mark.asyncio
async def test_publish(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)
    built = await _work_with_cited_paragraph(engine, corpus, "spine-publish", doc_id)
    spine = built["spine"]

    await spine.publish.freeze(slug="spine-publish", message="reviewed")
    sealed = await spine.publish.publish(slug="spine-publish")

    assert sealed.state == "published"
    report = await spine.validate.validate(slug="spine-publish", gate="publish")
    assert report.gate.passed is True


@pytest.mark.asyncio
async def test_quote_quota_is_per_document_and_blocks_only_publish(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    total = MAX_QUOTE_CHARS + 2
    long_text = ("licensed source words " * 100)[:total]
    long_doc = await _ingest_quoted_text(
        engine,
        corpus,
        title="One licensed source",
        edition_key="LICENSE-LONG",
        text=long_text,
    )

    async def cited_work(
        slug: str, sources: list[tuple[UUID, str, str]]
    ) -> _Spine:
        spine = _Spine(engine)
        created = await spine.works.create(
            slug=slug, title=slug, work_type="essay"
        )
        corpus.track(works, created.work_id)
        for position, (document_id, edition_key, quote) in enumerate(sources):
            block = await spine.works.upsert_block(
                slug=slug,
                position=position,
                block_type="paragraph",
                body_markdown=f"Source {position}.",
            )
            attached = await spine.cite.attach(
                slug=slug,
                block_key=block.block_key,
                intent="support",
                quote=quote,
                document_id=document_id,
                edition_key=edition_key,
            )
            corpus.adopt_span(attached.item.source_span_id)
            await spine.works.upsert_block(
                slug=slug,
                position=position,
                block_type="paragraph",
                body_markdown=f"Source {position}. {attached.marker}",
                block_key=block.block_key,
                expected_updated_at=block.updated_at,
            )
        return spine

    over = await cited_work(
        "license-over", [(long_doc, "LICENSE-LONG", long_text)]
    )
    draft = await over.validate.validate(slug="license-over", gate="none")
    [finding] = [
        item for item in draft.findings if item.rule_id == "AUTH_LICENSE_EXPORT"
    ]
    assert finding.severity == "warning"
    assert finding.detail is not None
    assert finding.detail["document_id"] == str(long_doc)
    assert finding.detail["quoted_characters"] == total
    assert finding.detail["cap"] == MAX_QUOTE_CHARS
    assert len(finding.detail["citation_keys"]) == 1
    assert draft.gate.passed
    assert (
        await over.validate.validate(slug="license-over", gate="freeze")
    ).gate.passed
    allow_policy = _Spine(
        engine, policy={"essay": {"AUTH_LICENSE_EXPORT": "allow"}}
    )
    allowed = await allow_policy.validate.validate(slug="license-over", gate="freeze")
    assert next(
        item for item in allowed.findings if item.rule_id == "AUTH_LICENSE_EXPORT"
    ).severity == "warning"
    tighten_policy = _Spine(
        engine, policy={"essay": {"AUTH_LICENSE_EXPORT": "error"}}
    )
    tightened = await tighten_policy.validate.validate(
        slug="license-over", gate="freeze"
    )
    assert "AUTH_LICENSE_EXPORT" in tightened.gate.blockers
    publish = await over.validate.validate(slug="license-over", gate="publish")
    assert publish.gate.passed is False
    assert "AUTH_LICENSE_EXPORT" in publish.gate.blockers
    assert next(
        item for item in publish.findings if item.rule_id == "AUTH_LICENSE_EXPORT"
    ).severity == "error"

    half = total // 2
    first_text = ("first document words " * 100)[:half]
    second_text = ("second document words " * 100)[: total - half]
    first_doc = await _ingest_quoted_text(
        engine,
        corpus,
        title="Split source one",
        edition_key="LICENSE-SPLIT-ONE",
        text=first_text,
    )
    second_doc = await _ingest_quoted_text(
        engine,
        corpus,
        title="Split source two",
        edition_key="LICENSE-SPLIT-TWO",
        text=second_text,
    )
    split = await cited_work(
        "license-split",
        [
            (first_doc, "LICENSE-SPLIT-ONE", first_text),
            (second_doc, "LICENSE-SPLIT-TWO", second_text),
        ],
    )
    split_report = await split.validate.validate(
        slug="license-split", gate="publish"
    )
    assert "AUTH_LICENSE_EXPORT" not in {
        item.rule_id for item in split_report.findings
    }
    assert split_report.gate.passed


@pytest.mark.asyncio
async def test_block_update_accepts_json_serialized_timestamp(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    spine = _Spine(engine)
    created = await spine.works.create(
        slug="spine-json-lock", title="JSON lock", work_type="essay"
    )
    corpus.track(works, created.work_id)
    block = await spine.works.upsert_block(
        slug="spine-json-lock",
        position=0,
        block_type="paragraph",
        body_markdown="Before.",
    )

    response = await dispatch_tool(
        SimpleNamespace(work_service=spine.works),
        "work_block_upsert",
        {
            "slug": "spine-json-lock",
            "block_key": str(block.block_key),
            "position": 0,
            "block_type": "paragraph",
            "body_markdown": "After.",
            "expected_updated_at": block.model_dump(mode="json")["updated_at"],
        },
    )

    assert "error" not in response
    stored = await spine.repos.blocks.by_key(
        created.revision_id, block.block_key
    )
    assert stored is not None
    assert stored.body_markdown == "After."
