"""Phase-0 works against the real corpus: verify, cite, key, and source block.

A fixture document (the Step 1 lexicon text, two passages split mid-sentence)
plus a fixture work written into a tmp works dir with the document's id. The
work is built so each entry lands on a different finding:

- c1 exact quotation, key matching: clean
- c2 straight-quote typing of a curly sentence: normalized, clean
- c3 background citing a whole passage: region, allowed
- c4 quotation citing a whole passage: AUTH_SPAN_NOT_NARROWED
- c5 changed word: AUTH_QUOTE_UNVERIFIED (near)
- c6 absent quote: AUTH_QUOTE_UNVERIFIED (not_found)
- c7 quote straddling the passage boundary: exact, clean
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event
from typer.testing import CliRunner

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGClaimRepo,
    PGDocumentNodeRepo,
    PGDocumentRepo,
    PGDocumentTextRepo,
    PGPassageRepo,
    PGSourceSpanRepo,
)
from research_engine.cli.work import work_app
from research_engine.domain.common import FusionMode
from research_engine.domain.passages import PassageDraft, SearchQuery
from research_engine.mcp.tools import work_citations
from research_engine.services.argument import AnchorContextService
from research_engine.services.search.hit_source import HitSourceReader
from research_engine.services.search.hybrid import HybridSearchService
from research_engine.services.verification import QuoteVerifier
from research_engine.services.works.citations import WorkCitationFinder
from research_engine.services.works.files import WorkFileReader
from research_engine.services.works.verify import WorkVerifier

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

_FIXTURES = Path(__file__).parent / "fixtures" / "works"
TEXT = (_FIXTURES / "lexicon_fixture.txt").read_text(encoding="utf-8")
SIDECAR = json.loads((_FIXTURES / "lexicon_fixture.json").read_text(encoding="utf-8"))
WORK_TEMPLATE = (_FIXTURES / "fixture_work.md").read_text(encoding="utf-8")

DOC_KEY = "DABAR_2026"


async def _ingest(
    engine: AsyncEngine, corpus: Corpus, metadata: dict[str, Any] | None = None
) -> UUID:
    """The lexicon document. None means the default key; pass {} for no key."""
    doc_id = await corpus.add_document(
        title="Dabaris",
        metadata={"edition_key": DOC_KEY} if metadata is None else metadata,
    )
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


def _write_work(tmp_path: Path, doc_id: UUID, name: str = "fragment.md") -> str:
    assert "__DOCUMENT_ID__" in WORK_TEMPLATE
    (tmp_path / name).write_text(
        WORK_TEMPLATE.replace("__DOCUMENT_ID__", str(doc_id)), encoding="utf-8"
    )
    return name


def _verifier(engine: AsyncEngine, works_dir: Path) -> WorkVerifier:
    return WorkVerifier(
        PGDocumentTextRepo(engine),
        PGDocumentRepo(engine),
        PGPassageRepo(engine),
        QuoteVerifier(
            PGDocumentTextRepo(engine), PGPassageRepo(engine), PGDocumentRepo(engine)
        ),
        PGClaimRepo(engine),
        works_dir,
    )


def _by_citation(report) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for citation in report.citations:
        out[citation.id] = sorted(citation.findings)
    return out


@pytest.mark.asyncio
async def test_verify_reports_each_finding(
    engine: AsyncEngine, corpus: Corpus, tmp_path: Path
) -> None:
    doc_id = await _ingest(engine, corpus)
    name = _write_work(tmp_path, doc_id)

    report = await _verifier(engine, tmp_path).verify_work(name, "review")

    assert report.work == "W-001"
    assert _by_citation(report) == {
        "c1": [],
        "c2": [],
        "c3": [],
        "c4": ["AUTH_SPAN_NOT_NARROWED"],
        "c5": ["AUTH_QUOTE_UNVERIFIED"],
        "c6": ["AUTH_QUOTE_UNVERIFIED"],
        "c7": [],
    }
    assert [finding.rule_id for finding in report.findings].count(
        "AUTH_CLAIM_UNRESOLVED"
    ) == 1
    assert report.findings[-1].severity == "error"
    assert not report.gate.passed
    assert report.gate.blockers == [
        "AUTH_CLAIM_UNRESOLVED",
        "AUTH_QUOTE_UNVERIFIED",
        "AUTH_SPAN_NOT_NARROWED",
    ]


@pytest.mark.asyncio
async def test_tier_honesty_holds_through_work_verify(
    engine: AsyncEngine, corpus: Corpus, tmp_path: Path
) -> None:
    """The curly-quote entry reports normalized, never exact."""
    doc_id = await _ingest(engine, corpus)
    name = _write_work(tmp_path, doc_id)

    report = await _verifier(engine, tmp_path).verify_work(name)

    tiers = {citation.id: citation.tier for citation in report.citations}
    assert tiers["c1"] == "exact"
    assert tiers["c2"] == "normalized"


@pytest.mark.asyncio
async def test_edition_mismatch(
    engine: AsyncEngine, corpus: Corpus, tmp_path: Path
) -> None:
    doc_id = await _ingest(engine, corpus, metadata={"edition_key": "OTHER_2026"})
    # Rewrite the entries' key expectation implicitly: entries carry DOC_KEY,
    # the document carries OTHER_2026.
    name = _write_work(tmp_path, doc_id)

    report = await _verifier(engine, tmp_path).verify_work(name)

    assert _by_citation(report)["c1"] == ["AUTH_EDITION_KEY_MISMATCH"]


@pytest.mark.asyncio
async def test_set_key_clears_the_unknown_finding(
    engine: AsyncEngine, corpus: Corpus, tmp_path: Path
) -> None:
    doc_id = await _ingest(engine, corpus, metadata={})
    name = _write_work(tmp_path, doc_id)
    verifier = _verifier(engine, tmp_path)
    before = await verifier.verify_work(name)
    assert "AUTH_EDITION_KEY_UNKNOWN" in _by_citation(before)["c1"]

    runner = CliRunner()
    result = await asyncio.to_thread(
        runner.invoke, work_app, ["set-key", str(doc_id), DOC_KEY]
    )

    assert result.exit_code == 0, result.output
    assert DOC_KEY in result.output
    stored = await PGDocumentRepo(engine).get(doc_id)
    assert stored is not None and stored.metadata.get("edition_key") == DOC_KEY
    after = await verifier.verify_work(name)
    assert "AUTH_EDITION_KEY_UNKNOWN" not in _by_citation(after)["c1"]


@pytest.mark.asyncio
async def test_set_key_refuses_an_unknown_document() -> None:
    runner = CliRunner()
    result = await asyncio.to_thread(
        runner.invoke, work_app, ["set-key", str(uuid.uuid4()), DOC_KEY]
    )

    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_citations_find_the_fixture_work(
    engine: AsyncEngine, corpus: Corpus, tmp_path: Path
) -> None:
    doc_id = await _ingest(engine, corpus)
    name = _write_work(tmp_path, doc_id)
    finder = WorkCitationFinder(tmp_path)

    by_document = await finder.find(document_id=doc_id)
    by_edition = await finder.find(edition_key=DOC_KEY)
    by_claim = await finder.find(claim_ref="TEST-001")

    assert {match["citation_id"] for match in by_document["matches"]} == {
        "c1", "c2", "c3", "c4", "c5", "c6", "c7",
    }
    assert all(match["work_path"] == name for match in by_document["matches"])
    assert by_document["source"] == "files"
    assert {match["citation_id"] for match in by_edition["matches"]} == {
        "c1", "c2", "c3", "c4", "c5", "c6", "c7",
    }
    assert len(by_claim["matches"]) == 1
    assert by_claim["matches"][0]["work_path"] == name

    missing = await finder.find(edition_key="NO_SUCH_KEY")
    assert missing["matches"] == []

    contextual = await work_citations.handler(
        SimpleNamespace(
            work_files=WorkFileReader(tmp_path),
            works_mirror_available=False,
            anchor_context_service=AnchorContextService(
                PGSourceSpanRepo(engine),
                PGDocumentTextRepo(engine),
                PGDocumentNodeRepo(engine),
            ),
        ),
        document_id=str(doc_id),
        context=True,
    )
    assert len(contextual["matches"]) == 7
    assert all("context" in match for match in contextual["matches"])
    for match in contextual["matches"]:
        item_context = match["context"]
        offset = item_context["quote_offset_in_window"]
        length = item_context["quote_length"]
        assert item_context["window"][offset : offset + length] == TEXT[
            match["char_start"] : match["char_end"]
        ]


# A token the live dev corpus does not contain, so the query below matches
# only the two fixture passages however much else the corpus holds.
SOURCE_TEXT = "Xyloglossa words open this entry. Xyloglossa words close that entry."


@pytest.mark.asyncio
async def test_find_passages_hits_carry_the_source_block(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """The citation draft rides the hit, read once per page, not per hit."""
    doc_id = await corpus.add_document(
        title="Commons",
        metadata={"edition_key": DOC_KEY, "author": "Kittel"},
    )
    split = len(SOURCE_TEXT) // 2
    async with transaction(engine) as tx:
        texts = PGDocumentTextRepo(engine)
        passages = PGPassageRepo(engine)
        await texts.put(tx, doc_id, SOURCE_TEXT, "test", "9.9")
        rows = await passages.insert_many(
            tx,
            doc_id,
            [
                PassageDraft(
                    position=0, char_start=0, char_end=split,
                    text=SOURCE_TEXT[:split], chunker="test", chunker_version="1.0",
                ),
                PassageDraft(
                    position=1, char_start=split, char_end=len(SOURCE_TEXT),
                    text=SOURCE_TEXT[split:], chunker="test", chunker_version="1.0",
                ),
            ],
        )
        await passages.index_fts(
            tx, [row.id for row in rows], [row.text for row in rows], "english"
        )
    service = HybridSearchService(
        passages=PGPassageRepo(engine),
        embedding=None,  # type: ignore[arg-type]
        reranker=None,  # type: ignore[arg-type]
        hit_sources=HitSourceReader(PGDocumentRepo(engine), PGDocumentTextRepo(engine)),
    )

    seen: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", listener)
    try:
        result = await service.find_passages(
            SearchQuery(text="xyloglossa", fusion_mode=FusionMode.keyword_only, rerank=False)
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", listener)

    ours = [hit for hit in result.hits if hit.document_id == doc_id]
    assert len(ours) == 2
    for hit in ours:
        assert hit.source is not None
        assert hit.source.edition_key == DOC_KEY
        assert hit.source.parser_version == "9.9"
        assert hit.source.has_canonical_text is True
        assert hit.source.has_offsets is True
    # One batched query per page: the documents table and the text table are
    # each read once for both hits, never once per hit.
    assert sum("FROM core.documents" in sql for sql in seen) == 1
    assert sum("FROM core.document_texts" in sql for sql in seen) == 1
