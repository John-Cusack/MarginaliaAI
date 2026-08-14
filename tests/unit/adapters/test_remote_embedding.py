"""The remote embedding client, exercised against the real server app.

Both ends run in-process over ASGI, so these cover the actual wire contract
rather than a mock's idea of it.

What the client must refuse matters more than what it returns. An embedding is
only comparable to embeddings from the same model; storing vectors from a
different model at the same dimension produces a defect no constraint catches
and no error reports — search simply gets worse.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from research_engine.adapters.embedding.remote_api import RemoteEmbeddingClient
from research_engine.adapters.embedding.wire import ModelMismatch

pytestmark = pytest.mark.unit

MODEL = "BAAI/bge-m3"
DIM = 8


class StubBackend:
    """Stands in for LocalBGEEmbedding so tests stay off the GPU."""

    def __init__(self, model: str = MODEL, dim: int = DIM, version: str = "1.0") -> None:
        self.model_name = model
        self.dim = dim
        self.model_version = version
        self.calls: list[list[str]] = []
        self.fail_with: Exception | None = None

    def _ensure_model(self) -> Any:
        return type("M", (), {"parameters": lambda self: iter([])})()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self.fail_with:
            raise self.fail_with
        self.calls.append(list(texts))
        return [[float(len(t))] * self.dim for t in texts]

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    """The real FastAPI app, with the model swapped for a stub."""
    backend = StubBackend()

    import research_engine.adapters.embedding.local_bge as local_bge

    monkeypatch.setattr(local_bge, "LocalBGEEmbedding", lambda *a, **k: backend)

    from research_engine.adapters.embedding.server import create_app

    app = create_app(MODEL, DIM, warm=False)
    return app, backend


def client_for(app: Any, **kwargs: Any) -> RemoteEmbeddingClient:
    client = RemoteEmbeddingClient(
        "http://test", kwargs.pop("model", MODEL), kwargs.pop("dim", DIM), **kwargs
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001 - inject the ASGI transport
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    return client


async def test_round_trip(server) -> None:
    app, backend = server
    client = client_for(app)
    try:
        vectors = await client.embed_batch(["alpha", "beta"])
        assert len(vectors) == 2
        assert all(len(v) == DIM for v in vectors)
        assert backend.calls == [["alpha", "beta"]]
    finally:
        await client.close()


async def test_health_reports_model_identity(server) -> None:
    app, _ = server
    client = client_for(app)
    try:
        health = await client.health()
        assert health.model_name == MODEL
        assert health.dim == DIM
        assert health.status == "ok"
    finally:
        await client.close()


async def test_client_refuses_a_different_model(server) -> None:
    """The defect this whole handshake exists to prevent."""
    app, _ = server
    client = client_for(app, model="some-other-model")
    try:
        with pytest.raises(ModelMismatch):
            await client.embed_batch(["alpha"])
    finally:
        await client.close()


async def test_server_refuses_a_client_expecting_another_model(server) -> None:
    """Enforced at both ends: the client can be wrong about itself."""
    app, _ = server
    client = client_for(app)
    client._model_name = MODEL  # noqa: SLF001 - client thinks it matches
    try:
        resp = await client._client.post(  # noqa: SLF001
            "/embeddings",
            json={"texts": ["x"], "expect_model": "wrong-model", "expect_dim": DIM},
        )
        assert resp.status_code == 409
        assert "not comparable" in resp.json()["detail"]
    finally:
        await client.close()


async def test_dimension_mismatch_is_refused(server) -> None:
    app, _ = server
    client = client_for(app, dim=1024)
    try:
        with pytest.raises(ModelMismatch):
            await client.embed_batch(["alpha"])
    finally:
        await client.close()


async def test_nothing_is_returned_before_the_handshake_succeeds(server) -> None:
    """No vector may reach the caller until identity is confirmed."""
    app, backend = server
    client = client_for(app, model="mismatched")
    try:
        with pytest.raises(ModelMismatch):
            await client.embed_batch(["alpha"])
        assert backend.calls == []
    finally:
        await client.close()


async def test_circuit_opens_after_repeated_failures() -> None:
    """A dead server must not cost `timeout x batches` before anyone notices."""

    async def always_fails(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = RemoteEmbeddingClient("http://test", MODEL, DIM, failure_threshold=2)
    client._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(always_fails), base_url="http://test"
    )
    client._verified = True  # noqa: SLF001 - skip the handshake for this test
    try:
        for _ in range(2):
            with pytest.raises(httpx.ConnectError):
                await client.embed_batch(["x"])

        # Third call fails fast with an actionable message rather than hanging.
        with pytest.raises(RuntimeError, match="refusing further calls"):
            await client.embed_batch(["x"])
    finally:
        await client.close()


async def test_a_success_resets_the_failure_count(server) -> None:
    app, _ = server
    client = client_for(app, failure_threshold=2)
    try:
        client._consecutive_failures = 1  # noqa: SLF001
        await client.embed_batch(["alpha"])
        assert client._consecutive_failures == 0  # noqa: SLF001
    finally:
        await client.close()


async def test_server_reports_memory_pressure_as_retryable(server) -> None:
    """503, not 500 — the client's halving retry is the right response."""
    app, backend = server
    backend.fail_with = RuntimeError("CUDA out of memory")
    client = client_for(app)
    try:
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await client.embed_batch(["alpha"])
        assert exc.value.response.status_code == 503
    finally:
        await client.close()


async def test_empty_batch_short_circuits(server) -> None:
    app, backend = server
    client = client_for(app)
    try:
        assert await client.embed_batch([]) == []
        assert backend.calls == []
    finally:
        await client.close()
