"""Where inference runs, and what an unreachable GPU host means.

The behaviour under test is the answer to a question that used to have a bad
one: a single `RE_EMBEDDING_BASE_URL` decided placement, model identity and
failure policy at once, so a sleeping desktop took down search entirely.
"""

from __future__ import annotations

import pytest

from research_engine.adapters.embedding.wire import ModelMismatch
from research_engine.adapters.inference.routing import (
    QueryEmbeddingWithFallback,
    build_inference,
)
from research_engine.config.settings import Settings
from research_engine.domain.errors import ConfigurationError, EmbeddingUnavailable

HOST = "http://gpu-host:9882"


class FakeEmbedding:
    model_name = "BAAI/bge-m3"
    model_version = "1.0"
    dim = 1024

    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises
        self.calls = 0

    async def embed_batch(self, texts):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return [[0.5] * self.dim for _ in texts]

    async def embed(self, text):
        return (await self.embed_batch([text]))[0]


class TestPlacement:
    def test_local_mode_ignores_a_configured_host(self):
        """The off switch must not require deleting the address.

        Previously `use_remote = provider == "remote_api" or base_url`, so the
        URL's mere presence forced remote and the only way to disable offload
        was to destroy the setting.
        """
        backends = build_inference(
            Settings(embedding_provider="local_bge", inference_base_url=HOST)
        )
        assert "local" in backends.summary
        assert backends.query_embedding is backends.bulk_embedding

    def test_remote_without_a_host_is_a_configuration_error(self):
        with pytest.raises(ConfigurationError, match="RE_INFERENCE_BASE_URL"):
            build_inference(Settings(embedding_provider="remote_api"))

    def test_auto_without_a_host_runs_local(self):
        """`auto` means "offload if you can", and here there is nothing to."""
        backends = build_inference(
            Settings(embedding_provider="auto", reranker_provider="auto")
        )
        assert "local" in backends.summary

    def test_auto_gives_query_a_fallback_and_bulk_none(self):
        backends = build_inference(
            Settings(embedding_provider="auto", inference_base_url=HOST)
        )
        assert isinstance(backends.query_embedding, QueryEmbeddingWithFallback)
        # Bulk is the bare remote client: a corpus-wide run that silently moved
        # to a laptop would take days instead of hours, unattended.
        assert not isinstance(backends.bulk_embedding, QueryEmbeddingWithFallback)

    def test_deprecated_embedding_base_url_still_resolves(self):
        settings = Settings(embedding_provider="auto", embedding_base_url=HOST)
        assert settings.resolved_inference_base_url == HOST


class TestQueryFallback:
    @pytest.mark.asyncio
    async def test_falls_back_to_local_when_the_host_is_unreachable(self):
        remote = FakeEmbedding(raises=EmbeddingUnavailable("host is asleep"))
        local = FakeEmbedding()
        routed = QueryEmbeddingWithFallback(
            remote, lambda: local, base_url=HOST
        )

        vector = await routed.embed("mishpat")

        assert len(vector) == 1024
        assert local.calls == 1

    @pytest.mark.asyncio
    async def test_does_not_build_the_local_model_when_remote_works(self):
        """The local model is 2.3 GB. A healthy setup must never pay for it."""
        built = []

        def build_local():
            built.append(True)
            return FakeEmbedding()

        routed = QueryEmbeddingWithFallback(
            FakeEmbedding(), build_local, base_url=HOST
        )
        await routed.embed("mishpat")

        assert built == []

    @pytest.mark.asyncio
    async def test_model_mismatch_is_not_papered_over(self):
        """A wrong model is a misconfiguration, not an outage.

        Falling back here would hide the one failure the handshake exists to
        catch, and hide it permanently, since a mismatch never heals on its own.
        """
        remote = FakeEmbedding(raises=ModelMismatch("bge-m3", "e5-large"))
        routed = QueryEmbeddingWithFallback(
            remote, lambda: FakeEmbedding(), base_url=HOST
        )

        with pytest.raises(ModelMismatch):
            await routed.embed("mishpat")

    @pytest.mark.asyncio
    async def test_bulk_embedding_never_falls_back(self):
        backends = build_inference(
            Settings(embedding_provider="auto", inference_base_url=HOST)
        )
        backends.bulk_embedding._client = None  # force a hard failure  # noqa: SLF001

        with pytest.raises(Exception):  # noqa: B017, PT011 - any failure, never a silent local run
            await backends.bulk_embedding.embed_batch(["x"])
