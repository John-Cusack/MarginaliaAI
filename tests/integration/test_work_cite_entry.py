"""Making citations against the real corpus: verified quotes resolve to rows."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGDocumentRepo,
    PGDocumentTextRepo,
    PGPassageRepo,
    PGSourceSpanRepo,
)
from research_engine.adapters.storage.postgres.schema import source_spans
from research_engine.cli.work import work_app
from research_engine.domain.passages import PassageDraft
from research_engine.services.verification import QuoteVerifier
from research_engine.services.works.cite import QuoteUnverifiedError, WorkCiter

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

_FIXTURES = Path(__file__).parent / "fixtures" / "works"
TEXT = (_FIXTURES / "lexicon_fixture.txt").read_text(encoding="utf-8")
SIDECAR = json.loads((_FIXTURES / "lexicon_fixture.json").read_text(encoding="utf-8"))
PROBES = SIDECAR["probes"]


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


def _citer(engine: AsyncEngine) -> WorkCiter:
    return WorkCiter(
        QuoteVerifier(
            PGDocumentTextRepo(engine), PGPassageRepo(engine), PGDocumentRepo(engine)
        ),
        PGSourceSpanRepo(engine),
        engine,
    )


@pytest.mark.asyncio
async def test_exact_quote_resolves_and_reuses_its_row(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)
    citer = _citer(engine)

    first = await citer.cite(
        document_id=doc_id,
        quoted_text=PROBES["exact"],
        intent="quotation",
        citation_id="c1",
    )
    second = await citer.cite(
        document_id=doc_id, quoted_text=PROBES["exact"], intent="quotation"
    )

    assert first.tier == "exact"
    assert first.verified_span == [34, 62]
    assert first.entry["char_start"] == 34
    assert first.entry["quoted_text"] == PROBES["exact"]
    assert first.span_id == second.span_id
    corpus.adopt_span(uuid.UUID(first.span_id))
    stored = await PGSourceSpanRepo(engine).get(first.span_id)
    assert stored is not None and stored.quoted_text == TEXT[34:62]


@pytest.mark.asyncio
async def test_typed_quote_cites_at_verified_offsets(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)

    result = await _citer(engine).cite(
        document_id=doc_id,
        quoted_text=PROBES["normalized_typed"],
        intent="quotation",
    )

    assert result.tier == "normalized"
    assert result.verified_span == [63, 163]
    assert result.entry["quoted_text"] == PROBES["normalized_typed"]
    corpus.adopt_span(uuid.UUID(result.span_id))


@pytest.mark.asyncio
async def test_window_pins_a_repeated_quote(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """Without a window the first occurrence wins; with one, the pinned one."""
    text = "Alpha beta. " + "Padding. " * 60 + "Alpha beta. "
    second_at = text.index("Alpha beta.", 100)
    doc_id = await corpus.add_document(title="Echoes")
    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(tx, doc_id, text, "test", "1.0")

    citer = _citer(engine)
    first_wins = await citer.cite(
        document_id=doc_id, quoted_text="Alpha beta.", intent="quotation"
    )
    pinned = await citer.cite(
        document_id=doc_id,
        quoted_text="Alpha beta.",
        intent="quotation",
        window=(second_at - 5, second_at + 16),
    )

    assert first_wins.verified_span == [0, 11]
    assert pinned.verified_span == [second_at, second_at + 11]
    corpus.adopt_span(uuid.UUID(first_wins.span_id))
    corpus.adopt_span(uuid.UUID(pinned.span_id))


@pytest.mark.asyncio
async def test_changed_quote_refuses_and_stores_nothing(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)

    with pytest.raises(QuoteUnverifiedError):
        await _citer(engine).cite(
            document_id=doc_id, quoted_text=PROBES["near"], intent="quotation"
        )

    assert await PGSourceSpanRepo(engine).for_document(doc_id) == []


@pytest.mark.asyncio
async def test_cli_cite_prints_a_paste_ready_entry(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)

    runner = CliRunner()
    result = await asyncio.to_thread(
        runner.invoke,
        work_app,
        ["cite-entry", "--document", str(doc_id), "--quote", PROBES["exact"],
         "--intent", "quotation", "--id", "c1"],
    )

    assert result.exit_code == 0, result.output
    assert "verified exact" in result.output
    assert "char_start: 34" in result.output
    async with engine.connect() as conn:
        span_id = (
            await conn.execute(
                sa.select(source_spans.c.id).where(
                    source_spans.c.document_id == doc_id
                )
            )
        ).scalar_one()
    corpus.adopt_span(span_id)
