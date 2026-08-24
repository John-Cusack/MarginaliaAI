"""Embedding offloaded to a GPU host over HTTP.

Runs `research-engine embed-server` on a machine with a fast card and points the
engine at it, so a laptop with a small GPU stops being the constraint on
corpus-wide work.

**Fallback deliberately means "fail", not "embed locally".** The vidgen TTS
offload falls back to local synthesis when its server is unreachable, and that is
right for audio: the voice changes slightly and the run completes. Embeddings are
not like that. A vector is only comparable to vectors from the same model, so
silently switching models mid-run would write points that no index can relate to
each other — undetectable by any constraint, and visible only as search slowly
getting worse. When the server is unreachable this raises, the batch fails, and
`research-engine embeddings backfill` picks it up later against a model that is
known to match.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import structlog

from research_engine.adapters.embedding.wire import (
    EmbedRequest,
    EmbedResponse,
    HealthResponse,
    ModelMismatch,
)
from research_engine.domain.errors import EmbeddingUnavailable, describe_exception

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = structlog.get_logger()

#: Consecutive failures before the circuit opens and calls fail fast instead of
#: each waiting out the timeout. A corpus-wide run issues thousands of calls;
#: without this, a server that died mid-run costs `timeout x batches` before
#: anyone notices.
FAILURE_THRESHOLD = 3


class RemoteEmbeddingClient:
    """An EmbeddingPort backed by a remote `research-engine embed-server`.

    Model identity is verified once, on the first call, against the server's
    ``/health``. Until that handshake succeeds no vector is stored.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        dim: int = 1024,
        *,
        model_version: str = "1.0",
        timeout: float = 120.0,
        api_key: str | None = None,
        failure_threshold: int = FAILURE_THRESHOLD,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model
        self._model_version = model_version
        self._dim = dim
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=self._base_url, headers=headers, timeout=timeout
        )
        self._verified: bool | None = None
        self._consecutive_failures = 0
        self._failure_threshold = failure_threshold
        self._lock = asyncio.Lock()

    # --- EmbeddingPort -----------------------------------------------------

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        # Before the handshake, not after. `_ensure_verified` dials /health, so
        # checking the circuit second meant a corpus run against a powered-off
        # host paid a connect timeout per batch — the exact cost the breaker
        # exists to avoid.
        self._check_circuit()
        await self._ensure_verified()
        self._check_circuit()

        payload = EmbedRequest(
            texts=list(texts),
            expect_model=self._model_name,
            expect_dim=self._dim,
        )
        try:
            resp = await self._client.post("/embeddings", json=payload.model_dump())
            resp.raise_for_status()
        except httpx.TransportError as exc:
            # Never reached the server, so batch size had nothing to do with it.
            # Translated here, at the layer that knows about transports, so the
            # caller can tell "the box is off" from "that batch was too big".
            self._consecutive_failures += 1
            raise EmbeddingUnavailable(
                f"Cannot reach the embedding server at {self._base_url}: "
                f"{describe_exception(exc)}"
            ) from exc
        except Exception:
            self._consecutive_failures += 1
            raise
        self._consecutive_failures = 0

        result = EmbedResponse.model_validate(resp.json())
        self._assert_matches(result.model_name, result.model_version, result.dim)
        if len(result.embeddings) != len(texts):
            raise RuntimeError(
                f"Remote server returned {len(result.embeddings)} embeddings for "
                f"{len(texts)} texts."
            )
        return result.embeddings

    def _check_circuit(self) -> None:
        if self._consecutive_failures >= self._failure_threshold:
            raise EmbeddingUnavailable(
                f"Remote embedding server {self._base_url} failed "
                f"{self._consecutive_failures} times consecutively; refusing "
                f"further calls. Fix the server, or set "
                f"RE_EMBEDDING_PROVIDER=local_bge to embed on this machine, "
                f"then run `research-engine embeddings backfill`."
            )

    async def close(self) -> None:
        await self._client.aclose()

    # --- Handshake ---------------------------------------------------------

    async def health(self) -> HealthResponse:
        resp = await self._client.get("/health", timeout=10.0)
        resp.raise_for_status()
        return HealthResponse.model_validate(resp.json())

    async def _ensure_verified(self) -> None:
        """Confirm once that the server serves the model this corpus uses."""
        if self._verified:
            return
        async with self._lock:
            if self._verified:
                return
            try:
                health = await self.health()
            except httpx.TransportError as exc:
                # The handshake is the *first* call a run makes, so an
                # unreachable host fails here rather than in `embed_batch`.
                # Untranslated it surfaced as a bare ConnectTimeout and was
                # mistaken for a batch that needed halving.
                self._consecutive_failures += 1
                raise EmbeddingUnavailable(
                    f"Cannot reach the embedding server at {self._base_url}: "
                    f"{describe_exception(exc)}. Check the host is powered on "
                    f"and `research-engine embed-server` is running."
                ) from exc
            self._assert_matches(health.model_name, health.model_version, health.dim)
            self._verified = True
            logger.info(
                "remote_embedding_verified",
                base_url=self._base_url,
                model=health.model_name,
                dim=health.dim,
                device=health.device,
                concurrency=health.concurrency,
            )

    def _assert_matches(self, name: str, version: str, dim: int) -> None:
        if name != self._model_name:
            raise ModelMismatch(self._model_name, name)
        if dim != self._dim:
            raise ModelMismatch(
                f"{self._model_name} (dim {self._dim})",
                f"{name} (dim {dim})",
                "Dimension disagreement would be caught by the vector(N) column; "
                "a same-dimension model mismatch would not.",
            )
        if version != self._model_version:
            # Not fatal on its own — a version bump may be a packaging change —
            # but it is exactly the kind of drift that explains a later recall
            # regression, so it must not pass silently.
            logger.warning(
                "remote_embedding_version_drift",
                expected=self._model_version,
                actual=version,
            )


#: Backwards-compatible alias. The old name described a stub that spoke a
#: generic OpenAI-shaped API and verified nothing.
RemoteAPIEmbedding = RemoteEmbeddingClient
