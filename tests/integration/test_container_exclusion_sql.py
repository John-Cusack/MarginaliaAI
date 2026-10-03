"""The vector and keyword queries leave out passages of excluded types.

This is the unfiltered search path: no candidate list exists to subtract from,
so the exclusion has to live inside both queries, and both have to agree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories.passages import PGPassageRepo
from research_engine.testing.corpus import new_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

MODEL, VERSION, DIM = "exclusion-test-model", "1.0", 1024


async def a_volume_and_its_letter(engine: AsyncEngine, corpus: Corpus, word: str):
    repo = PGPassageRepo(engine)
    text = f"FARADAY TO HIS MOTHER. {word} July 1."
    volume = await corpus.add_document(document_type="letter_collection")
    letter = await corpus.add_document(document_type="letter")
    in_volume = await corpus.add_passage(volume, text)
    in_letter = await corpus.add_passage(letter, text)
    async with transaction(engine) as tx:
        await repo.index_fts(tx, [in_volume, in_letter], [text, text], "english")
        await repo.store_embeddings(
            tx, [in_volume, in_letter], [[0.5] * DIM, [0.5] * DIM], MODEL, VERSION, DIM
        )
    return repo, in_volume, in_letter


async def test_keyword_search_leaves_the_container_out(engine: AsyncEngine, corpus: Corpus):
    word = f"zq{new_id().hex[:10]}"
    repo, in_volume, in_letter = await a_volume_and_its_letter(engine, corpus, word)

    everything = {pid for pid, _ in await repo.keyword_search(word, "english", None, 10)}
    default = {
        pid
        for pid, _ in await repo.keyword_search(
            word, "english", None, 10, exclude_document_types=["letter_collection"]
        )
    }

    assert everything == {in_volume, in_letter}
    assert default == {in_letter}


async def test_vector_search_leaves_the_container_out(engine: AsyncEngine, corpus: Corpus):
    repo, in_volume, in_letter = await a_volume_and_its_letter(engine, corpus, "Geneva")

    everything = {
        pid
        for pid, _ in await repo.vector_search(
            [0.5] * DIM, MODEL, VERSION, [in_volume, in_letter], 10
        )
    }
    default = {
        pid
        for pid, _ in await repo.vector_search(
            [0.5] * DIM,
            MODEL,
            VERSION,
            [in_volume, in_letter],
            10,
            exclude_document_types=["letter_collection"],
        )
    }

    assert everything == {in_volume, in_letter}
    assert default == {in_letter}
