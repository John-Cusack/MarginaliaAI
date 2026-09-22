"""The Rust fusion seam must be bit-identical to the Python it replaces.

Every vector runs under both ``RE_RUST_BACKEND=rust`` and ``=python``; the
two answers must compare equal — scores as exact bit patterns (the 1-ulp
``1/65`` history means floats are never compared loosely here), breakdowns
as equal dicts, pids as the *same objects*. Exact-score ties in
``weighted_fuse`` are the documented exception: upstream iterates a ``set``
(order varies run to run), so ties compare per-id, not row order.

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from research_engine.domain.passages import SearchQuery
from research_engine.services.search import fusion as fusion_py
from research_engine.services.search import hybrid as hybrid_mod
from research_engine.services.search.hybrid import HybridSearchService

IDS = [uuid.UUID(f"12345678-1234-5678-1234-5678123456{n:02d}") for n in range(6)]

#: Scores chosen for the seams that have bitten before: a 17-digit decimal
#: (the ``repr(1/65)`` 1-ulp parse gap — these cross as binary f64, never
#: decimals), negative zero, and integral floats.
TRICKY_SCORES = [1 / 65, -0.0, 3.0, 0.1 + 0.2, 1e-5, 1e16]


def _fuse_all(vec, kw, alpha=0.5, k=60):
    return (
        hybrid_mod._rrf_fuse(vec, kw, k=k),
        hybrid_mod._weighted_fuse(vec, kw, alpha=alpha),
    )


def _bits(rows):
    return [(str(pid), score.hex(), bd) for pid, score, bd in rows]


class TestFusionParity:
    @pytest.mark.parametrize("alpha", [0.0, 0.3, 0.5, 1.0])
    @pytest.mark.parametrize("k", [60, 7])
    def test_rust_matches_python_bit_exact(self, alpha, k, monkeypatch):
        pytest.importorskip("marginalia_rs")
        vec = [(pid, s) for pid, s in zip(IDS[:4], [0.95, 0.8, 1 / 65, -0.0], strict=True)]
        kw = [(pid, s) for pid, s in zip(IDS[1:5], [0.9, 0.85, 3.0, 0.1 + 0.2], strict=True)]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _fuse_all(vec, kw, alpha=alpha, k=k)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = _fuse_all(vec, kw, alpha=alpha, k=k)
        assert _bits(actual[0]) == _bits(expected[0])
        assert [pid for pid, _, _ in actual[0]] == [pid for pid, _, _ in expected[0]]
        # The ignored `k` must not move scores.
        assert _bits(actual[0]) == _bits(hybrid_mod._rrf_fuse(vec, kw, k=60))

    def test_empty_and_single_sided(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _fuse_all([], [], k=7)
        expected_single = hybrid_mod._weighted_fuse([(IDS[0], 0.9)], [], alpha=0.5)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert hybrid_mod._rrf_fuse([], [], k=7) == expected[0] == []
        assert hybrid_mod._weighted_fuse([], []) == expected[1] == []
        assert _bits(hybrid_mod._weighted_fuse([(IDS[0], 0.9)], [], alpha=0.5)) == _bits(
            expected_single
        )

    def test_weighted_ties_compare_per_id(self, monkeypatch):
        """Exact ties: scores per id are the contract, row order is not."""
        pytest.importorskip("marginalia_rs")
        vec = [(IDS[0], 1.0)]
        kw = [(IDS[1], 2.0)]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = {str(pid): score.hex() for pid, score, _ in hybrid_mod._weighted_fuse(vec, kw)}
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = {str(pid): score.hex() for pid, score, _ in hybrid_mod._weighted_fuse(vec, kw)}
        assert actual == expected

    def test_output_pids_are_the_input_objects(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        vec = [(IDS[0], 0.9), (IDS[1], 0.5)]
        kw = [(IDS[1], 1.4), (IDS[2], 0.2)]
        for row in hybrid_mod._rrf_fuse(vec, kw):
            assert any(row[0] is pid for pid in IDS)
        for row in hybrid_mod._weighted_fuse(vec, kw):
            assert any(row[0] is pid for pid in IDS)

    def test_python_path_equals_direct_calls(self, monkeypatch):
        """Rollback pin: forced-python is exactly the old behavior."""
        vec = [(IDS[0], 0.9)]
        kw = [(IDS[1], 0.8)]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert hybrid_mod._rrf_fuse(vec, kw, k=7) == fusion_py.rrf_fuse(vec, kw, k=7)
        assert hybrid_mod._weighted_fuse(vec, kw, alpha=0.3) == fusion_py.weighted_fuse(
            vec, kw, alpha=0.3
        )

    def test_non_uuid_pids_rejected_on_rust(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ValueError, match="UUID"):
            hybrid_mod._rrf_fuse([("nope", 1.0)], [])
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert hybrid_mod._rrf_fuse([("nope", 1.0)], []) != []


class TestHybridParity:
    def _service(self):
        doc = uuid.uuid4()

        class Passages:
            async def vector_search(self, vec, model, version, candidates, k):
                return [(pid, 1.0 - i * 0.1) for i, pid in enumerate(IDS[:k])]

            async def keyword_search(self, text, lang, candidates, k):
                return [(pid, 0.5) for pid in IDS[:k]]

            def _passage(self, pid):
                return SimpleNamespace(
                    id=pid,
                    document_id=doc,
                    text=f"passage {pid}",
                    metadata={},
                    locator={},
                    char_start=0,
                    char_end=60,
                    node_id=None,
                )

            async def get_many(self, passage_ids):
                return [self._passage(pid) for pid in passage_ids]

        class Embedding:
            model_name, model_version, dim = "m", "1", 8

            async def embed(self, text):
                return [0.1] * self.dim

        class Reranker:
            async def rerank(self, query, passage_ids, texts, k):
                return [(pid, 1.0) for pid in passage_ids[:k]]

        return HybridSearchService(passages=Passages(), embedding=Embedding(), reranker=Reranker())

    @pytest.mark.parametrize("fusion_mode", ["rrf", "weighted"])
    @pytest.mark.parametrize("rerank", [False, True])
    async def test_find_passages_matches_across_backends(self, fusion_mode, rerank, monkeypatch):
        pytest.importorskip("marginalia_rs")
        service = self._service()
        query = SearchQuery(text="mishpat", k=3, fusion_mode=fusion_mode, rerank=rerank)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await service.find_passages(query)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await service.find_passages(query)
        assert actual.model_dump() == expected.model_dump()
