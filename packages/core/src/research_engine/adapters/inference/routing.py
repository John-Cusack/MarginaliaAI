"""Where inference runs, and what happens when that place is unreachable.

A single ``RE_EMBEDDING_BASE_URL`` used to decide three unrelated things at
once: where compute happens (a placement decision, and a genuinely dynamic one
— a desktop sleeps), which model is authoritative (a corpus invariant that must
never drift), and whether a failure is fatal (a per-call-site policy). Setting
the URL silently answered all three, and the only way to answer the first one
differently was to delete the address.

This module separates them.

**Placement** is `*_provider`, and the mode wins over the URL's presence, so
`local_bge` is an off switch you can flip without losing the host address.

**Model identity** is not defended here at all, because it is already defended
better elsewhere: `passage_embeddings` is keyed by `(passage_id, model,
model_version)` and search only ever reads rows matching the live embedder. A
wrong-model vector does not corrupt the index, it lands in a namespace nothing
queries. What genuinely needs catching is a backend that *lies* about its
identity, and that is the `/health` handshake's job.

**Failure policy** is the part worth thinking about, and it is not the
read/write split it first appears to be. Falling back to a backend that passes
the same identity handshake is safe — local and remote bge-m3 were measured
bit-identical, `max|Δ| = 0.00e+00`. What differs is the *cost of degrading*:

- A query embedding is one short string. Falling back costs a model load and
  then 66 ms. Search stays up; nobody needs to know.
- A corpus-wide embed is 255k passages. Falling back turns hours on a 3090 into
  days on a laptop. That must stop and say so, not quietly downgrade.
- A rerank has a third option the others do not: skip it. On a host without a
  working accelerator, reranking 30 candidates measured 48.8 s of a 49.1 s
  search. Both obvious answers are bad — failing the search outright, or
  silently spending 49 seconds — so the search degrades in *quality, visibly*
  instead of in *latency, invisibly*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from research_engine.domain.errors import ConfigurationError, EmbeddingUnavailable

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from research_engine.config.settings import Settings
    from research_engine.ports.embedding import EmbeddingPort
    from research_engine.ports.reranker import RerankerPort

logger = structlog.get_logger()


class Workload(StrEnum):
    """What a caller is doing, which is what decides the failure policy."""

    #: One short text, a person waiting. Degrading beats failing.
    QUERY = "query"
    #: Corpus-wide, unattended, results are stored. Failing beats degrading,
    #: because a silent downgrade here costs days and nobody is watching.
    BULK = "bulk"


class QueryEmbeddingWithFallback:
    """Remote embedding that falls back to local rather than failing a search.

    Only ever wired into the query path. The local model is built on first need,
    not up front, so a healthy remote setup never pays the 2.3 GB load.

    Safe because both ends run the model named by `RE_EMBEDDING_MODEL` and the
    remote end has proved it on `/health`; the vectors are interchangeable. It
    is `EmbeddingUnavailable` alone that triggers this — a `ModelMismatch` is a
    misconfiguration, and quietly papering over it is how a corpus ends up half
    embedded by something nobody chose.
    """

    def __init__(
        self,
        remote: EmbeddingPort,
        build_local: Callable[[], EmbeddingPort],
        *,
        base_url: str,
    ) -> None:
        self._remote = remote
        self._build_local = build_local
        self._local: EmbeddingPort | None = None
        self._base_url = base_url
        self._warned = False

    @property
    def model_name(self) -> str:
        return self._remote.model_name

    @property
    def model_version(self) -> str:
        return self._remote.model_version

    @property
    def dim(self) -> int:
        return self._remote.dim

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return await self._remote.embed_batch(list(texts))
        except EmbeddingUnavailable as exc:
            if not self._warned:
                # Once per process. A warning on every keystroke-fast query
                # would bury the one line that explains the slowdown.
                logger.warning(
                    "query_embedding_fell_back_to_local",
                    base_url=self._base_url,
                    error=str(exc),
                    detail="Queries are being embedded on this machine. Corpus "
                    "ingestion still requires the GPU host and will refuse to "
                    "run without it.",
                )
                self._warned = True
            if self._local is None:
                self._local = self._build_local()
            return await self._local.embed_batch(list(texts))


@dataclass
class InferenceBackends:
    """The adapters the composition root wires in, plus how to shut them down."""

    query_embedding: EmbeddingPort
    bulk_embedding: EmbeddingPort
    reranker: RerankerPort
    summary: str
    closeables: list[Any] = field(default_factory=list)

    async def close(self) -> None:
        for target in self.closeables:
            closer = getattr(target, "close", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning("inference_close_failed", error=str(exc))


def build_inference(settings: Settings) -> InferenceBackends:
    """Build embedding and reranking adapters according to the configured modes."""
    from research_engine.adapters.embedding.local_bge import LocalBGEEmbedding
    from research_engine.adapters.embedding.remote_api import RemoteEmbeddingClient
    from research_engine.adapters.reranker.local_bge import LocalBGEReranker
    from research_engine.adapters.reranker.noop import NoopReranker
    from research_engine.adapters.reranker.remote_api import RemoteReranker

    base_url = settings.resolved_inference_base_url
    if settings.inference_base_url is None and settings.embedding_base_url:
        logger.info(
            "inference_base_url_deprecated_alias",
            detail="RE_EMBEDDING_BASE_URL still works; RE_INFERENCE_BASE_URL is "
            "the current name, since one server now serves both models.",
        )

    api_key = (
        settings.embedding_api_key.get_secret_value()
        if settings.embedding_api_key
        else None
    )

    # Shared and lazy: the local model is 2.3 GB, and the query-fallback path
    # and a local bulk path must not each load their own copy.
    _local_cache: dict[str, EmbeddingPort] = {}

    def local_embedding() -> EmbeddingPort:
        if "e" not in _local_cache:
            _local_cache["e"] = LocalBGEEmbedding(
                settings.embedding_model, settings.embedding_dim
            )
        return _local_cache["e"]

    closeables: list[Any] = []
    embed_mode = _resolve_mode(
        settings.embedding_provider, base_url, what="embedding"
    )

    if embed_mode == "local_bge":
        query_embedding = bulk_embedding = local_embedding()
        embed_summary = f"embedding local ({settings.embedding_model})"
    else:
        assert base_url is not None  # _resolve_mode guarantees it
        remote = RemoteEmbeddingClient(
            base_url,
            settings.embedding_model,
            settings.embedding_dim,
            timeout=settings.embedding_timeout,
            api_key=api_key,
        )
        closeables.append(remote)
        # Bulk never falls back, in either remote mode. A corpus run that
        # silently moved to the laptop would finish next week.
        bulk_embedding = remote
        if embed_mode == "auto":
            query_embedding = QueryEmbeddingWithFallback(
                remote, local_embedding, base_url=base_url
            )
            embed_summary = f"embedding {base_url}, queries fall back to local"
        else:
            query_embedding = remote
            embed_summary = f"embedding {base_url} (no fallback)"

    rerank_mode = _resolve_mode(
        settings.reranker_provider, base_url, what="reranker"
    )

    if rerank_mode == "none":
        reranker: RerankerPort = NoopReranker()
        rerank_summary = "reranking disabled"
    elif rerank_mode == "local_bge":
        reranker = LocalBGEReranker(settings.reranker_model)
        rerank_summary = f"reranking local ({settings.reranker_model})"
    else:
        assert base_url is not None
        reranker = RemoteReranker(
            base_url,
            settings.reranker_model,
            timeout=settings.reranker_timeout,
            api_key=api_key,
            # `remote_api` insists on that host; `auto` merely prefers it, so a
            # server too old to offer /rerank degrades rather than erroring.
            require_capability=rerank_mode == "remote_api",
        )
        closeables.append(reranker)
        # Both remote modes behave the same at call time: an outage means the
        # search returns unreranked and flagged. They differ only in what
        # happens with no URL configured, which `_resolve_mode` has settled.
        rerank_summary = f"reranking {base_url}, skipped if unreachable"

    summary = f"{embed_summary}; {rerank_summary}"
    logger.info("inference_configured", summary=summary)
    return InferenceBackends(
        query_embedding=query_embedding,
        bulk_embedding=bulk_embedding,
        reranker=reranker,
        summary=summary,
        closeables=closeables,
    )


def _resolve_mode(mode: str, base_url: str | None, *, what: str) -> str:
    """Settle a configured mode against whether a host address actually exists.

    `remote_api` without an address is a contradiction the operator has to fix.
    `auto` without one is not — it means "offload if you can", and there is
    nothing to offload to, so it resolves to local and says so.
    """
    if mode in {"local_bge", "none"}:
        if base_url:
            logger.debug(
                "inference_base_url_ignored",
                what=what,
                mode=mode,
                detail=f"RE_{what.upper()}_PROVIDER={mode} overrides the "
                f"configured host. The address is kept, not used.",
            )
        return mode
    if base_url:
        return mode
    if mode == "remote_api":
        raise ConfigurationError(
            f"RE_{what.upper()}_PROVIDER=remote_api but no host is configured. "
            f"Set RE_INFERENCE_BASE_URL to a `research-engine embed-server`, "
            f"e.g. http://john-super-server:9882 — or set the provider to "
            f"'auto' to run locally when no host is reachable."
        )
    logger.info(
        "inference_auto_resolved_local",
        what=what,
        detail="RE_INFERENCE_BASE_URL is unset, so 'auto' runs on this machine.",
    )
    return "local_bge"
