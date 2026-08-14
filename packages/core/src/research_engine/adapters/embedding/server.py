"""HTTP server that lends a fast GPU to a slower machine.

Run on the box with the big card::

    research-engine embed-server --host 0.0.0.0 --port 9882 --warm

and point the engine at it::

    RE_EMBEDDING_PROVIDER=remote_api
    RE_EMBEDDING_BASE_URL=http://john-super-server:9882

Design follows the vidgen TTS server: one warm model, a semaphore bounding GPU
occupancy, and the blocking work pushed off the event loop with
``asyncio.to_thread`` so ``/health`` stays responsive while a large batch is in
flight. A client that cannot get a timely ``/health`` cannot distinguish a busy
server from a dead one, and will open its circuit against a machine that is
merely working.

The server is stateless. It reports which model it serves and rejects requests
that expect a different one, so a misconfigured client fails on its first call
rather than after depositing thousands of incomparable vectors.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from research_engine.adapters.embedding.wire import (
    EmbedRequest,
    EmbedResponse,
    HealthResponse,
)

logger = structlog.get_logger()


def create_app(
    model: str = "BAAI/bge-m3",
    dim: int = 1024,
    *,
    device: str | None = None,
    warm: bool = False,
    concurrency: int = 1,
) -> Any:
    """Build the ASGI app wrapping a warm embedding model.

    Args:
        model: Model to serve. Must match the client's `RE_EMBEDDING_MODEL`.
        dim: Vector width to report and truncate to.
        device: Torch device override; None auto-detects.
        warm: Load weights at startup rather than on first request, so the first
            real batch does not pay for it.
        concurrency: Batches allowed on the GPU at once. 1 is safest. A 24 GB
            card can usually take 2; beyond that long passages risk CUDA OOM,
            which the client handles by halving but at the cost of a round trip.
    """
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:  # pragma: no cover - surfaced through the CLI
        raise ImportError(
            "FastAPI is required to run the embed server. Install with: "
            'uv pip install "research-engine[embed-server]"'
        ) from exc

    from contextlib import asynccontextmanager

    from research_engine.adapters.embedding.local_bge import LocalBGEEmbedding

    backend = LocalBGEEmbedding(model, dim)
    state: dict[str, Any] = {"warm": False, "device": device or "auto"}
    gpu = asyncio.Semaphore(concurrency)

    def _load() -> str:
        loaded = backend._ensure_model()  # noqa: SLF001 - the adapter owns loading
        try:
            return str(next(loaded.parameters()).device)
        except Exception:  # noqa: BLE001 - device reporting is best-effort
            return device or "unknown"

    @asynccontextmanager
    async def lifespan(_app: Any):
        if warm:
            logger.info("embed_server_warming", model=model)
            state["device"] = await asyncio.to_thread(_load)
            state["warm"] = True
            logger.info("embed_server_ready", model=model, device=state["device"])
        yield

    app = FastAPI(title="research-engine embed-server", lifespan=lifespan)

    @app.get("/health")
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            model_name=backend.model_name,
            model_version=backend.model_version,
            dim=backend.dim,
            device=str(state["device"]),
            warm=bool(state["warm"]),
            concurrency=concurrency,
        )

    @app.post("/embeddings")
    async def embeddings(request: EmbedRequest) -> EmbedResponse:
        if request.expect_model and request.expect_model != backend.model_name:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This server serves {backend.model_name!r}; the client "
                    f"expects {request.expect_model!r}. Vectors from different "
                    f"models are not comparable — refusing rather than serving "
                    f"them."
                ),
            )
        if request.expect_dim and request.expect_dim != backend.dim:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This server serves dim {backend.dim}; the client expects "
                    f"{request.expect_dim}."
                ),
            )

        async with gpu:
            try:
                vectors = await backend.embed_batch(list(request.texts))
            except Exception as exc:  # noqa: BLE001 - report, do not crash the server
                logger.error(
                    "embed_server_batch_failed",
                    size=len(request.texts),
                    error=str(exc),
                )
                # 503 rather than 500: the client's halving retry is the right
                # response to memory pressure, and a smaller batch may succeed.
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        if not state["warm"]:
            state["warm"] = True

        return EmbedResponse(
            embeddings=vectors,
            model_name=backend.model_name,
            model_version=backend.model_version,
            dim=backend.dim,
        )

    return app
