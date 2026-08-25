"""Reranking on the GPU host, and what a search does when it cannot get there.

Reranking is 99.4% of query latency on a host without a working accelerator
(48.8 s of a 49.1 s search, measured on this corpus at rerank_n=30). That makes
both obvious failure responses wrong: erroring out loses an answer the engine
could still give, and running it locally anyway costs 49 seconds with no warning.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from research_engine.adapters.embedding.wire import ModelMismatch
from research_engine.adapters.reranker.remote_api import RemoteReranker
from research_engine.adapters.reranker.scoring import rank_from_scores
from research_engine.domain.errors import ConfigurationError, RerankUnavailable

RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
HOST = "http://gpu-host:9882"


# --- shared scoring ---------------------------------------------------------


class TestRankFromScores:
    def test_orders_by_score_descending(self):
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        assert rank_from_scores([a, b, c], [0.1, 0.9, 0.5], 3) == [
            (b, 0.9), (c, 0.5), (a, 0.1),
        ]

    def test_ties_keep_fusion_order(self):
        """An exact tie falls back to what RRF thought, which is real signal."""
        a, b = uuid.uuid4(), uuid.uuid4()
        assert rank_from_scores([a, b], [0.7, 0.7], 2) == [(a, 0.7), (b, 0.7)]

    def test_length_mismatch_is_refused(self):
        """Scores are positional; a mismatch mis-attributes every result."""
        with pytest.raises(ValueError, match="pairing is wrong"):
            rank_from_scores([uuid.uuid4(), uuid.uuid4()], [0.5], 2)


# --- the server side --------------------------------------------------------


class FakeEmbedding:
    model_name, model_version, dim = "BAAI/bge-m3", "1.0", 1024

    def __init__(self, *a, **kw) -> None: ...
    def _ensure_model(self): return SimpleNamespace(parameters=lambda: iter([]))
    async def embed_batch(self, texts): return [[0.1] * self.dim for _ in texts]


class FakeReranker:
    model_version = "1.0"

    def __init__(self, model_name=RERANK_MODEL) -> None:
        self.model_name = model_name

    def _ensure_model(self): return None

    async def score(self, query, texts):
        # Length-proportional, so a test can tell input order from sorted order.
        return [float(len(t)) for t in texts]


@pytest.fixture
def patched_models(monkeypatch):
    monkeypatch.setattr(
        "research_engine.adapters.embedding.local_bge.LocalBGEEmbedding", FakeEmbedding
    )
    monkeypatch.setattr(
        "research_engine.adapters.reranker.local_bge.LocalBGEReranker", FakeReranker
    )


class TestInferenceServer:
    def test_health_advertises_both_models(self, patched_models):
        from research_engine.adapters.embedding.server import create_app

        with TestClient(create_app(rerank_model=RERANK_MODEL)) as client:
            body = client.get("/health").json()
        assert body["model_name"] == "BAAI/bge-m3"
        assert body["rerank_model"] == RERANK_MODEL

    def test_embedding_only_server_says_so_rather_than_404ing_silently(
        self, patched_models
    ):
        """A client must tell "not offered here" from "down"; only one is fixable."""
        from research_engine.adapters.embedding.server import create_app

        with TestClient(create_app(rerank_model=None)) as client:
            assert client.get("/health").json()["rerank_model"] is None
            assert client.post(
                "/rerank", json={"query": "q", "texts": ["a"]}
            ).status_code == 404

    def test_scores_come_back_in_input_order(self, patched_models):
        from research_engine.adapters.embedding.server import create_app

        with TestClient(create_app(rerank_model=RERANK_MODEL)) as client:
            body = client.post(
                "/rerank", json={"query": "q", "texts": ["aaa", "a", "aa"]}
            ).json()
        assert body["scores"] == [3.0, 1.0, 2.0]

    def test_rerank_model_mismatch_is_refused(self, patched_models):
        from research_engine.adapters.embedding.server import create_app

        with TestClient(create_app(rerank_model=RERANK_MODEL)) as client:
            resp = client.post(
                "/rerank",
                json={"query": "q", "texts": ["a"], "expect_model": "other-model"},
            )
        assert resp.status_code == 409


# --- the client side --------------------------------------------------------


def _client(handler, **kw) -> RemoteReranker:
    remote = RemoteReranker(HOST, RERANK_MODEL, **kw)
    remote._client = httpx.AsyncClient(  # noqa: SLF001 - swap in a mock transport
        transport=httpx.MockTransport(handler), base_url=HOST
    )
    return remote


def _health(**overrides) -> dict:
    return {
        "status": "ok", "model_name": "BAAI/bge-m3", "model_version": "1.0",
        "dim": 1024, "device": "cuda:0", "warm": True, "concurrency": 1,
        "rerank_model": RERANK_MODEL, "rerank_model_version": "1.0",
        "rerank_warm": True, **overrides,
    }


class TestRemoteReranker:
    @pytest.mark.asyncio
    async def test_happy_path_ranks_by_score(self):
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json=_health())
            return httpx.Response(200, json={
                "scores": [0.2, 0.9], "model_name": RERANK_MODEL,
                "model_version": "1.0",
            })

        a, b = uuid.uuid4(), uuid.uuid4()
        ranked = await _client(handler).rerank("q", [a, b], ["x", "y"], k=2)
        assert ranked == [(b, 0.9), (a, 0.2)]

    @pytest.mark.asyncio
    async def test_unreachable_host_raises_rerank_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("host is asleep")

        with pytest.raises(RerankUnavailable, match="Cannot reach"):
            await _client(handler).rerank("q", [uuid.uuid4()], ["x"], k=1)

    @pytest.mark.asyncio
    async def test_a_slow_server_is_reported_as_slow_not_missing(self):
        """"Too slow" and "not there" have different fixes.

        A CPU-only host reranking 30 candidates takes ~26 s against a 30 s
        budget, so this is the failure a working-but-underpowered deployment
        actually hits — and "cannot reach" would send someone to check the
        network instead of the accelerator.
        """
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json=_health())
            raise httpx.ReadTimeout("too slow")

        with pytest.raises(RerankUnavailable, match="did not answer within"):
            await _client(handler).rerank("q", [uuid.uuid4()], ["x"], k=1)

    @pytest.mark.asyncio
    async def test_embedding_only_server_errors_when_remote_was_demanded(self):
        """`remote_api` names a host; one that cannot rerank contradicts that."""
        def handler(request):
            return httpx.Response(200, json=_health(rerank_model=None))

        with pytest.raises(ConfigurationError, match="--rerank-model"):
            await _client(handler, require_capability=True).rerank(
                "q", [uuid.uuid4()], ["x"], k=1
            )

    @pytest.mark.asyncio
    async def test_embedding_only_server_degrades_under_auto(self):
        """A server predating /rerank must not brick every search.

        `auto` means "offload if you can". Erroring here took down search
        entirely the first time a new client met an old server — which is the
        single most likely version skew, since the client is upgraded first.
        """
        def handler(request):
            return httpx.Response(200, json=_health(rerank_model=None))

        with pytest.raises(RerankUnavailable, match="--rerank-model"):
            await _client(handler, require_capability=False).rerank(
                "q", [uuid.uuid4()], ["x"], k=1
            )

    @pytest.mark.asyncio
    async def test_wrong_rerank_model_is_refused(self):
        def handler(request):
            return httpx.Response(200, json=_health(rerank_model="other-model"))

        with pytest.raises(ModelMismatch):
            await _client(handler).rerank("q", [uuid.uuid4()], ["x"], k=1)

    @pytest.mark.asyncio
    async def test_circuit_opens_so_every_query_does_not_wait_out_a_timeout(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            raise httpx.ConnectError("down")

        remote = _client(handler, failure_threshold=2)
        for _ in range(4):
            with pytest.raises(RerankUnavailable):
                await remote.rerank("q", [uuid.uuid4()], ["x"], k=1)

        # Stops dialling once the circuit is open, rather than paying the
        # connect timeout on every single search.
        assert len(calls) <= 2
