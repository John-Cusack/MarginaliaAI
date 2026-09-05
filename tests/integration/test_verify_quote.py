"""verify_quote against real Postgres, through the normalized-text column.

The unit suite covers the tier logic with fakes. What fakes cannot cover is
the path that only exists in SQL: `_locate_normalized` reads the stored
`normalized_text` column via `find_normalized`, estimates a raw offset from
the raw:normalized length ratio, and folds a window back to raw offsets.
A wrong column write, a bad ratio, or an off-by-one in the window would all
pass the fakes and fail here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGDocumentRepo,
    PGDocumentTextRepo,
    PGPassageRepo,
)
from research_engine.domain.passages import PassageDraft
from research_engine.services.verification import QuoteVerifier, Tier

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

_FIXTURES = Path(__file__).parent / "fixtures" / "works"
TEXT = (_FIXTURES / "lexicon_fixture.txt").read_text(encoding="utf-8")
SIDECAR = json.loads((_FIXTURES / "lexicon_fixture.json").read_text(encoding="utf-8"))
SPLIT = SIDECAR["passage_split"]
PROBES = SIDECAR["probes"]


async def _ingest(engine: AsyncEngine, corpus: Corpus) -> uuid.UUID:
    """One document holding TEXT, in two passages split mid-sentence."""
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


def _verifier(engine: AsyncEngine) -> QuoteVerifier:
    return QuoteVerifier(
        PGDocumentTextRepo(engine), PGPassageRepo(engine), PGDocumentRepo(engine)
    )


@pytest.mark.asyncio
async def test_typography_reports_normalized_never_exact(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    """Straight quotes, a hyphen for the em dash, no line-break hyphen."""
    doc_id = await _ingest(engine, corpus)

    result = await _verifier(engine).verify(PROBES["normalized_typed"], doc_id)

    assert result.tier is Tier.NORMALIZED
    assert result.tier is not Tier.EXACT
    assert result.verified


@pytest.mark.asyncio
async def test_exact_quote_reports_exact(engine: AsyncEngine, corpus: Corpus) -> None:
    doc_id = await _ingest(engine, corpus)

    result = await _verifier(engine).verify(PROBES["exact"], doc_id)

    assert result.tier is Tier.EXACT
    assert result.verified
    assert TEXT[result.location.char_start : result.location.char_end] == PROBES["exact"]


@pytest.mark.asyncio
async def test_changed_word_reports_near_with_source_continuation(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)

    result = await _verifier(engine).verify(PROBES["near"], doc_id)

    assert result.tier is Tier.NEAR
    assert not result.verified
    assert "king" in result.divergence.quote_continues
    # The source's continuation, not the quote's: what stands where "king" was.
    assert "ruler" in result.divergence.source_continues


@pytest.mark.asyncio
async def test_quote_crossing_a_passage_boundary_is_exact_and_straddles(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    quote = SIDECAR["spans"]["straddling"]["text"]
    doc_id = await _ingest(engine, corpus)

    result = await _verifier(engine).verify(quote, doc_id)

    assert result.tier is Tier.EXACT
    assert result.location.straddles_passages
    assert len(result.location.passage_ids) == 2
    assert TEXT[result.location.char_start : result.location.char_end] == quote


@pytest.mark.asyncio
async def test_document_with_no_text_is_not_not_found(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await corpus.add_document(title="Untextured")
    assert await PGDocumentTextRepo(engine).lengths(doc_id) is None

    result = await _verifier(engine).verify("anything at all", doc_id)

    assert result.tier is Tier.NO_CANONICAL_TEXT
    assert result.documents_checked == 0


@pytest.mark.asyncio
async def test_empty_quote_is_not_found(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)

    result = await _verifier(engine).verify("   ", doc_id)

    assert result.tier is Tier.NOT_FOUND


@pytest.mark.asyncio
async def test_absent_quote_is_not_found(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    doc_id = await _ingest(engine, corpus)

    result = await _verifier(engine).verify(PROBES["not_found"], doc_id)

    assert result.tier is Tier.NOT_FOUND


@pytest.mark.asyncio
async def test_fixture_sidecar_spans_slice_the_fixture_text() -> None:
    for name, span in SIDECAR["spans"].items():
        assert TEXT[span["char_start"] : span["char_end"]] == span["text"], name
    assert 0 < SPLIT < len(TEXT)
