"""A query that finds the GPU host down starts it, without waiting for it.

The bug this guards: only bulk commands ever started the embed unit, and both
remote clients' breakers stayed open for the life of the process. A long-lived
MCP server whose host idle-exited answered every later query on the CPU, and
unreranked, until someone restarted it.
"""

from __future__ import annotations

import asyncio

import pytest

from research_engine.adapters.embedding.remote_api import RemoteEmbeddingClient
from research_engine.adapters.inference.gpu_host import (
    QueryPathWaker,
    wakes_on_query,
)
from research_engine.adapters.inference.routing import (
    WAKE_HINT,
    QueryEmbeddingWithFallback,
    WakeOnUnavailableEmbedding,
    WakeOnUnavailableReranker,
    build_inference,
)
from research_engine.adapters.reranker.remote_api import RemoteReranker
from research_engine.config.settings import Settings
from research_engine.domain.errors import EmbeddingUnavailable, RerankUnavailable

pytestmark = pytest.mark.unit

LOCAL = "http://127.0.0.1:9882"
REMOTE = "http://gpu-host.example:9882"


def _settings(**overrides):
    base = {
        "embedding_provider": "remote_api",
        "reranker_provider": "auto",
        "inference_base_url": LOCAL,
    }
    base.update(overrides)
    return Settings(**base)


class FakeStart:
    def __init__(self, outcome: str = "started", raises: Exception | None = None):
        self.outcome = outcome
        self.raises = raises
        self.calls = 0
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self, _settings):
        self.calls += 1
        await self.release.wait()
        if self.raises is not None:
            raise self.raises
        return self.outcome


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _drain(waker: QueryPathWaker) -> None:
    if waker._task is not None:  # noqa: SLF001
        await waker._task  # noqa: SLF001


class TestWhoWakes:
    def test_loopback_host_wakes(self):
        assert wakes_on_query(_settings())

    def test_a_host_over_ssh_does_not(self):
        """A query never reaches across ssh to start someone else's GPU."""
        assert not wakes_on_query(_settings(inference_base_url=REMOTE))

    def test_never_means_never(self):
        assert not wakes_on_query(_settings(embed_manage_gpu="never"))

    def test_no_host_no_wake(self):
        assert not wakes_on_query(_settings(inference_base_url=None,
                                            embedding_provider="local_bge"))


class TestWaker:
    async def test_poke_starts_once_and_resets_breakers(self):
        start = FakeStart()
        waker = QueryPathWaker(_settings(), start=start)
        resets: list[str] = []
        waker.on_ready(lambda: resets.append("x"))

        waker.poke("test")
        await _drain(waker)

        assert start.calls == 1
        assert resets == ["x"]

    async def test_one_wake_in_flight_at_a_time(self):
        start = FakeStart()
        start.release.clear()
        waker = QueryPathWaker(_settings(), start=start, min_interval=0)

        waker.poke("a")
        await asyncio.sleep(0)
        waker.poke("b")
        start.release.set()
        await _drain(waker)

        assert start.calls == 1

    async def test_rate_limited_after_a_wake(self):
        start = FakeStart()
        clock = Clock()
        waker = QueryPathWaker(_settings(), start=start, min_interval=60, clock=clock)

        waker.poke("a")
        await _drain(waker)
        clock.now += 30
        waker.poke("b")
        await _drain(waker)
        assert start.calls == 1

        clock.now += 31
        waker.poke("c")
        await _drain(waker)
        assert start.calls == 2

    async def test_a_failed_start_is_swallowed_and_resets_nothing(self):
        waker = QueryPathWaker(_settings(), start=FakeStart(raises=RuntimeError("no")))
        resets: list[str] = []
        waker.on_ready(lambda: resets.append("x"))

        waker.poke("test")
        await _drain(waker)

        assert resets == []

    async def test_skipped_resets_nothing(self):
        waker = QueryPathWaker(_settings(), start=FakeStart(outcome="skipped"))
        resets: list[str] = []
        waker.on_ready(lambda: resets.append("x"))
        waker.poke("test")
        await _drain(waker)
        assert resets == []

    def test_poke_outside_a_loop_is_a_no_op(self):
        start = FakeStart()
        QueryPathWaker(_settings(), start=start).poke("sync")
        assert start.calls == 0


class Down:
    model_name = "BAAI/bge-m3"
    model_version = "1.0"
    dim = 1024

    async def embed_batch(self, texts):
        raise EmbeddingUnavailable("host down")

    async def rerank(self, query, passage_ids, texts, k):
        raise RerankUnavailable("host down")


class RecordingWaker:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def poke(self, reason: str) -> None:
        self.reasons.append(reason)


class TestWrappers:
    async def test_embedding_outage_pokes_and_says_so(self):
        waker = RecordingWaker()
        wrapped = WakeOnUnavailableEmbedding(Down(), waker)  # type: ignore[arg-type]
        with pytest.raises(EmbeddingUnavailable, match=WAKE_HINT):
            await wrapped.embed("q")
        assert waker.reasons == ["query_embedding_unavailable"]

    async def test_rerank_outage_pokes_and_still_degrades(self):
        waker = RecordingWaker()
        wrapped = WakeOnUnavailableReranker(Down(), waker)  # type: ignore[arg-type]
        with pytest.raises(RerankUnavailable):
            await wrapped.rerank("q", [], ["t"], 1)
        assert waker.reasons == ["rerank_unavailable"]


class TestWiring:
    async def test_local_host_wires_the_waker(self):
        backends = build_inference(_settings())
        try:
            assert isinstance(backends.query_embedding, WakeOnUnavailableEmbedding)
            assert isinstance(backends.reranker, WakeOnUnavailableReranker)
            assert isinstance(backends.bulk_embedding, RemoteEmbeddingClient)
            assert "start the GPU host" in backends.summary
        finally:
            await backends.close()

    async def test_auto_keeps_its_fallback_around_the_waker(self):
        backends = build_inference(_settings(embedding_provider="auto"))
        try:
            assert isinstance(backends.query_embedding, QueryEmbeddingWithFallback)
        finally:
            await backends.close()

    async def test_remote_host_is_left_alone(self):
        backends = build_inference(_settings(inference_base_url=REMOTE))
        try:
            assert isinstance(backends.query_embedding, RemoteEmbeddingClient)
            assert isinstance(backends.reranker, RemoteReranker)
        finally:
            await backends.close()


class TestBreakerReset:
    async def test_reset_closes_an_open_embedding_breaker(self):
        client = RemoteEmbeddingClient(REMOTE, "BAAI/bge-m3", 1024)
        try:
            client._consecutive_failures = 99  # noqa: SLF001
            client._verified = True  # noqa: SLF001
            with pytest.raises(EmbeddingUnavailable, match="consecutively"):
                client._check_circuit()  # noqa: SLF001
            client.reset_circuit()
            client._check_circuit()  # noqa: SLF001
            assert client._verified is None  # noqa: SLF001
        finally:
            await client.close()

    async def test_reset_closes_an_open_rerank_breaker(self):
        client = RemoteReranker(REMOTE, "BAAI/bge-reranker-v2-m3")
        try:
            client._consecutive_failures = 99  # noqa: SLF001
            with pytest.raises(RerankUnavailable):
                client._check_circuit()  # noqa: SLF001
            client.reset_circuit()
            client._check_circuit()  # noqa: SLF001
        finally:
            await client.close()
