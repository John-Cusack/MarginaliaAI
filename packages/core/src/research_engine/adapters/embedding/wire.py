"""Wire contract shared by the remote inference client and server.

Both ends import these models so the contract cannot drift — the pattern the
vidgen TTS offload uses, and the reason its client and server have never
disagreed about a field.

The thing this protocol exists to prevent is subtler than a network error. An
embedding is only meaningful relative to the model that produced it: vectors
from a different model at the same dimension are not *wrong* in any way the
database can detect, they are simply incomparable with everything already
stored, and search silently degrades. So the handshake carries model identity,
and the client refuses to store a single vector until it matches.

Reranking carries no such hazard — a score is consumed immediately and never
stored — but it travels the same wire because it is the same GPU and the same
outage. Measured on this corpus, reranking is 99.4% of query latency (48.8 s of
a 49.1 s search on a CPU-only host), so it is the offload that actually matters.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """``GET /health`` — identity and readiness of the served models."""

    status: str = "ok"
    model_name: str
    model_version: str
    dim: int
    device: str
    #: False while the model is still loading. The server answers /health before
    #: the weights are resident so a client can wait rather than time out.
    warm: bool = True
    #: How many embed requests may occupy the GPU at once.
    concurrency: int = 1

    #: Reranker identity, present only when this server also serves ``/rerank``.
    #: ``None`` means embedding-only: either an older deployment or one started
    #: without ``--rerank-model``. A client must be able to tell "this server
    #: does not do reranking" from "reranking is down", because the first is a
    #: deployment choice to respect and the second an outage to report.
    rerank_model: str | None = None
    rerank_model_version: str | None = None
    rerank_warm: bool = False


class EmbedRequest(BaseModel):
    """``POST /embeddings`` — texts to embed."""

    texts: list[str] = Field(min_length=1)
    #: The model the *client* believes it is talking to. The server rejects a
    #: mismatch rather than silently serving different vectors, so a
    #: misconfigured client fails on its first call instead of after 200k
    #: passages.
    expect_model: str | None = None
    expect_dim: int | None = None


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    model_name: str
    model_version: str
    dim: int


class RerankRequest(BaseModel):
    """``POST /rerank`` — score each text against the query.

    Deliberately returns scores in input order rather than a sorted ranking. The
    caller holds the passage ids and the fusion breakdown; making it re-associate
    a permuted list against those would be an easy place to introduce an
    off-by-one that no test would catch, because a mis-paired score still looks
    like a plausible ranking.
    """

    query: str
    texts: list[str] = Field(min_length=1)
    expect_model: str | None = None


class RerankResponse(BaseModel):
    #: One score per input text, in input order.
    scores: list[float]
    model_name: str
    model_version: str


class ModelMismatch(RuntimeError):
    """The server is not serving the model this corpus was built with.

    For embeddings this is a data-integrity failure: storing these vectors would
    put incomparable points in one index, a defect with no error message and no
    symptom beyond search quietly getting worse. For reranking it is milder —
    scores are transient — but it still means results are being ordered by a
    model the caller did not choose, so it is refused rather than logged.
    """

    def __init__(
        self, expected: str, actual: str, detail: str = "", *, kind: str = "embedding"
    ) -> None:
        self.expected = expected
        self.actual = actual
        self.kind = kind
        super().__init__(
            f"Remote {kind} server serves {actual!r}, but this corpus expects "
            f"{expected!r}. {detail}".strip()
        )
