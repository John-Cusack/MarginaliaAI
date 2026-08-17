"""Batch embedding: halve what halving can fix, stop for what it cannot.

A re-chunk once ran for hours against a GPU host that had been powered off for a
day. It halved 16 -> 8 -> 4 -> 2 -> 1 on every batch, marked each passage failed,
moved to the next document, and did that thousands of times — logging
``error=``, an empty string, because ``str(httpx.ConnectTimeout())`` is empty.
It wrote nothing and looked busy.

Both halves of that failure are asserted here: the classification (a backend
that is gone is not a batch that is too big) and the reporting (a log line that
names the cause).
"""

from __future__ import annotations

import httpx
import pytest

from research_engine.domain.errors import EmbeddingUnavailable, describe_exception
from research_engine.services.ingestion.embed_batches import embed_and_store
from uuid import uuid4


class _Embedder:
    """Fails every call with *exc*, counting attempts."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    async def embed_batch(self, texts):
        self.calls += 1
        raise self._exc


class _GoodEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed_batch(self, texts):
        self.calls += 1
        return [[0.0] * 4 for _ in texts]


async def _noop_store(ids, vectors) -> None:
    return None


def _batch(n: int):
    return [uuid4() for _ in range(n)], [f"passage {i}" for i in range(n)]


async def test_an_unreachable_backend_stops_the_run_instead_of_halving() -> None:
    """The defect itself: halving cannot reach a host that is not there."""
    ids, texts = _batch(16)
    embedder = _Embedder(EmbeddingUnavailable("server is not answering"))

    with pytest.raises(EmbeddingUnavailable):
        await embed_and_store(embedder, ids, texts, _noop_store)

    assert embedder.calls == 1, (
        f"tried {embedder.calls} times against an unreachable backend; the same "
        f"call fails identically at every size, so each retry costs a timeout "
        f"and buys nothing"
    )


async def test_an_ordinary_failure_still_halves() -> None:
    """The behaviour that must survive: memory pressure is fixed by halving."""
    ids, texts = _batch(16)
    embedder = _Embedder(RuntimeError("CUDA out of memory"))

    outcome = await embed_and_store(embedder, ids, texts, _noop_store)

    assert embedder.calls > 1, "gave up without halving on a recoverable failure"
    assert outcome.halvings > 0
    assert len(outcome.failed_passages) == 16


async def test_a_healthy_batch_is_stored_whole() -> None:
    ids, texts = _batch(8)
    stored: list[int] = []

    async def store(batch_ids, vectors):
        stored.append(len(batch_ids))

    embedder = _GoodEmbedder()
    outcome = await embed_and_store(embedder, ids, texts, store)

    assert outcome.embedded == 8
    assert stored == [8]
    assert embedder.calls == 1


def test_a_connect_timeout_still_describes_itself() -> None:
    """The reason the logs were empty for hours.

    `httpx.ConnectTimeout` carries no message, so `str(exc)` is ''. Anything
    that logs only `str(exc)` reports a blank cause for the single most likely
    remote-embedding failure there is.
    """
    exc = httpx.ConnectTimeout("")

    assert str(exc) == "", "httpx changed; this test's premise needs rechecking"
    assert describe_exception(exc) == "ConnectTimeout"


def test_a_described_exception_keeps_its_message_when_it_has_one() -> None:
    assert (
        describe_exception(httpx.ConnectError("All connection attempts failed"))
        == "ConnectError: All connection attempts failed"
    )
