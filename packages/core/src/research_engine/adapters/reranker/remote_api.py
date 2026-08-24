"""Reranking offloaded to a GPU host over HTTP.

The counterpart of `RemoteEmbeddingClient`, and the one that matters more for a
person actually using the engine. Query embedding is 66 ms locally against
roughly 20 ms remote — a wash. Reranking on a CPU-only host is 48.8 s of a
49.1 s search, so this is where the fast card earns its place.

Failure semantics differ from the embedding client, deliberately. That one
raises and stops the run, because a batch that does not embed leaves a passage
with no vector and a run that must be redone. Here, failing means refusing to
answer a question the engine could still answer reasonably well, so this raises
`RerankUnavailable` and the search service catches it, returns fused results and
marks them degraded.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import structlog

from research_engine.adapters.embedding.wire import (
    HealthResponse,
    ModelMismatch,
    RerankRequest,
    RerankResponse,
)
from research_engine.adapters.reranker.scoring import rank_from_scores
from research_engine.domain.errors import (
    ConfigurationError,
    RerankUnavailable,
    describe_exception,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

logger = structlog.get_logger()

#: Consecutive failures before the circuit opens. Lower than the embedding
#: client's because the cost of being wrong is lower: a query that skips
#: reranking still returns, so failing fast and degrading beats making every
#: search wait out a timeout while a laptop is on the wrong network.
FAILURE_THRESHOLD = 2

#: Reranking is interactive. A request that takes longer than this has already
#: failed the researcher whether or not it eventually returns.
DEFAULT_TIMEOUT = 30.0


class RemoteReranker:
    """A RerankerPort backed by a remote inference server's ``/rerank``."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        model_version: str = "1.0",
        timeout: float = DEFAULT_TIMEOUT,
        api_key: str | None = None,
        failure_threshold: int = FAILURE_THRESHOLD,
        require_capability: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model
        self._model_version = model_version
        self._timeout = timeout
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=self._base_url, headers=headers, timeout=timeout
        )
        self._verified: bool | None = None
        self._consecutive_failures = 0
        self._failure_threshold = failure_threshold
        self._require_capability = require_capability
        self._lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_version(self) -> str:
        return self._model_version

    async def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        # Before the handshake, not after. `_ensure_verified` dials /health, so
        # checking the circuit second meant a dead host was re-dialled on every
        # single query and the breaker never actually broke anything.
        self._check_circuit()
        await self._ensure_verified()
        self._check_circuit()

        payload = RerankRequest(
            query=query, texts=list(texts), expect_model=self._model_name
        )
        try:
            resp = await self._client.post("/rerank", json=payload.model_dump())
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            # Must precede TransportError, which it subclasses. Worth separating
            # because the two have different fixes and look identical otherwise:
            # a timeout means the server is there and too slow (a CPU-only host
            # reranking 30 candidates takes ~26 s against a 30 s budget), while
            # unreachable means it is not there at all.
            self._consecutive_failures += 1
            raise RerankUnavailable(
                f"Rerank server {self._base_url} did not answer within "
                f"{self._timeout:g}s. It is running but too slow for an "
                f"interactive query — check it has a working accelerator, or "
                f"raise RE_RERANKER_TIMEOUT if you would rather wait."
            ) from exc
        except httpx.TransportError as exc:
            self._consecutive_failures += 1
            raise RerankUnavailable(
                f"Cannot reach the rerank server at {self._base_url}: "
                f"{describe_exception(exc)}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            self._consecutive_failures += 1
            # A 409 is a model disagreement, not an outage, and retrying will
            # never fix it. Surfacing it as ModelMismatch keeps it out of the
            # "degrade quietly" path, where a permanent misconfiguration would
            # look like an intermittent network problem forever.
            if exc.response.status_code == 409:
                raise ModelMismatch(
                    self._model_name,
                    exc.response.text,
                    kind="rerank",
                ) from exc
            raise RerankUnavailable(
                f"Rerank server {self._base_url} returned "
                f"{exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except Exception:
            self._consecutive_failures += 1
            raise
        self._consecutive_failures = 0

        result = RerankResponse.model_validate(resp.json())
        if result.model_name != self._model_name:
            raise ModelMismatch(self._model_name, result.model_name, kind="rerank")
        if len(result.scores) != len(texts):
            raise RerankUnavailable(
                f"Rerank server returned {len(result.scores)} scores for "
                f"{len(texts)} texts."
            )
        return result.scores

    async def rerank(
        self, query: str, passage_ids: list[UUID], texts: list[str], k: int
    ) -> list[tuple[UUID, float]]:
        if not texts:
            return []
        return rank_from_scores(passage_ids, await self.score(query, texts), k)

    def _check_circuit(self) -> None:
        if self._consecutive_failures >= self._failure_threshold:
            raise RerankUnavailable(
                f"Rerank server {self._base_url} failed "
                f"{self._consecutive_failures} times consecutively; skipping "
                f"reranking until it recovers."
            )

    async def close(self) -> None:
        await self._client.aclose()

    # --- Handshake ---------------------------------------------------------

    async def health(self) -> HealthResponse:
        resp = await self._client.get("/health", timeout=10.0)
        resp.raise_for_status()
        return HealthResponse.model_validate(resp.json())

    async def _ensure_verified(self) -> None:
        if self._verified:
            return
        async with self._lock:
            if self._verified:
                return
            try:
                health = await self.health()
            except httpx.TransportError as exc:
                self._consecutive_failures += 1
                raise RerankUnavailable(
                    f"Cannot reach the rerank server at {self._base_url}: "
                    f"{describe_exception(exc)}. Check the host is powered on "
                    f"and `research-engine embed-server` is running."
                ) from exc

            if health.rerank_model is None:
                # Not an outage: the server is up and deliberately — or, more
                # often, accidentally — embedding-only. Which error depends on
                # what the operator asked for, and getting this wrong bricked
                # every search against a server that predated /rerank:
                #
                #   remote_api — "use that host". A host that cannot do the job
                #     contradicts an explicit instruction, so it is an error.
                #   auto       — "offload if you can". You cannot, so degrade
                #     and say why, the same as any other reason reranking is
                #     unavailable.
                message = (
                    f"The inference server at {self._base_url} serves embeddings "
                    f"but not reranking. Restart it with "
                    f"`--rerank-model {self._model_name}`, or set "
                    f"RE_RERANKER_PROVIDER=local_bge to rerank on this machine."
                )
                if self._require_capability:
                    raise ConfigurationError(message)
                self._consecutive_failures = self._failure_threshold
                raise RerankUnavailable(message)
            if health.rerank_model != self._model_name:
                raise ModelMismatch(
                    self._model_name, health.rerank_model, kind="rerank"
                )
            self._verified = True
            logger.info(
                "remote_rerank_verified",
                base_url=self._base_url,
                model=health.rerank_model,
                device=health.device,
            )
