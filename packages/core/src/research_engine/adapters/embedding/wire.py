"""Wire contract shared by the remote embedding client and server.

Both ends import these models so the contract cannot drift — the pattern the
vidgen TTS offload uses, and the reason its client and server have never
disagreed about a field.

The thing this protocol exists to prevent is subtler than a network error. An
embedding is only meaningful relative to the model that produced it: vectors
from a different model at the same dimension are not *wrong* in any way the
database can detect, they are simply incomparable with everything already
stored, and search silently degrades. So the handshake carries model identity,
and the client refuses to store a single vector until it matches.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """``GET /health`` — identity and readiness of the serving model."""

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


class ModelMismatch(RuntimeError):
    """The server is not serving the model this corpus is embedded with.

    Storing these vectors would put incomparable points in one index — a defect
    with no error message and no obvious symptom beyond search quietly getting
    worse.
    """

    def __init__(self, expected: str, actual: str, detail: str = "") -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Remote embedding server serves {actual!r}, but this corpus is "
            f"embedded with {expected!r}. {detail}".strip()
        )
