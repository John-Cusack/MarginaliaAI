"""The Rust eval seam must match the Python metrics.

`recall`/`precision`/`reciprocal_rank` are exactly rounded IEEE division on
both sides: hex-bit equality. `dcg`/`ndcg` accumulate, and CPython's
Neumaier-compensated `sum()` can differ from the crate's plain fold by a few
ulp even on short lists (measured: 1 ulp on `[0.5]*20`, 1 ulp on 6-term
`ndcg`; random gains first diverged at 278 terms) — irrelevant to baselines,
documented in both crates: exact on the short fixed battery, `isclose`
beyond. Inputs are typed UUIDs; anything else answers `ValueError` on Rust
where Python would hash garbage (pinned boundary).

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import math
import uuid

import pytest

from research_engine.eval import metrics as metrics_mod
from research_engine.eval.metrics import (
    dcg,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


def _hex(f: float) -> str:
    # `dcg([])` is int `0` on the Python path (bare `sum`) and `0.0`
    # from Rust: equal values, normalize the spelling to compare bits.
    return float(f).hex()


IDS = [uuid.UUID(f"12345678-1234-5678-1234-56781234{n:04d}") for n in range(12)]


class TestRankMetricParity:
    @pytest.mark.parametrize("k", [0, 1, 3, 10, 100])
    def test_recall_matches_bit_exact(self, k, monkeypatch):
        pytest.importorskip("marginalia_rs")
        retrieved, relevant = IDS[:7], IDS[2:9]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = recall_at_k(retrieved, relevant, k)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = recall_at_k(retrieved, relevant, k)
        assert _hex(actual) == _hex(expected)

    def test_recall_empty_judgments_scores_one(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert recall_at_k(IDS[:3], [], 10) == 1.0

    @pytest.mark.parametrize("k", [0, 1, 5, 10])
    def test_precision_matches_bit_exact(self, k, monkeypatch):
        pytest.importorskip("marginalia_rs")
        retrieved, relevant = IDS[:7], IDS[5:10]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = precision_at_k(retrieved, relevant, k)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = precision_at_k(retrieved, relevant, k)
        assert _hex(actual) == _hex(expected)

    def test_reciprocal_rank_matches_bit_exact(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = [
            reciprocal_rank(IDS[:7], IDS[3:5]),
            reciprocal_rank(IDS[:7], IDS[9:11]),
            reciprocal_rank([], IDS[:2]),
        ]
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = [
            reciprocal_rank(IDS[:7], IDS[3:5]),
            reciprocal_rank(IDS[:7], IDS[9:11]),
            reciprocal_rank([], IDS[:2]),
        ]
        assert [_hex(a) for a in actual] == [_hex(e) for e in expected]
        assert actual[0] == 1.0 / 4

    def test_non_uuid_ids_rejected_on_rust(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ValueError, match="UUID"):
            recall_at_k(["nope"], IDS[:2], 10)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert recall_at_k(["nope"], IDS[:2], 10) == 0.0


class TestGainMetricParity:
    @pytest.mark.parametrize(
        "gains",
        [
            [],
            [3.0],
            [3.0, 2.0, 1.0],
            [1.0, 0.0, 0.0, 2.5],
            [-1.0, 2.0, -0.5],
        ],
    )
    def test_dcg_matches_bit_exact_on_battery(self, gains, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = dcg(gains)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = dcg(gains)
        assert _hex(actual) == _hex(expected)

    @pytest.mark.parametrize(
        "gains",
        [
            [0.5] * 20,
            [0.5] * 64,
        ],
    )
    def test_dcg_accumulating_shapes_agree_to_comparison_precision(self, gains, monkeypatch):
        """The fold and Neumaier part by a few ulp on accumulating sums
        (measured: 1 ulp on `[0.5]*20`). Baselines compare far above that,
        so `isclose` is the contract here, not bits."""
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = dcg(gains)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = dcg(gains)
        assert math.isclose(actual, expected, rel_tol=1e-12)

    def test_dcg_long_lists_agree_to_comparison_precision(self, monkeypatch):
        """Past long accumulations the fold and Neumaier can part by a few
        ulp (measured: first at 278 random terms). Baselines compare far
        above that, so `isclose` is the contract there, not bits."""
        pytest.importorskip("marginalia_rs")
        import random

        random.seed(7)
        gains = [random.uniform(-5, 5) for _ in range(400)]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = dcg(gains)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = dcg(gains)
        assert math.isclose(actual, expected, rel_tol=1e-12)

    def test_ndcg_binary_and_graded_match(self, monkeypatch):
        """`ndcg` divides two `dcg` accumulations, so the fold-vs-Neumaier
        ulp (measured: 1 ulp on a 6-term binary case) reaches it even on
        short lists. Contract is `isclose`, not bits — still tight enough
        to catch any ranking, grading, or normalization bug."""
        pytest.importorskip("marginalia_rs")
        retrieved = IDS[:6]
        binary = IDS[2:8]
        graded = {IDS[2]: 2.0, IDS[3]: 1.0, IDS[9]: 0.5}
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = (
            ndcg_at_k(retrieved, binary, 10),
            ndcg_at_k(retrieved, graded, 5),
            ndcg_at_k(retrieved, {}, 10),
            ndcg_at_k(retrieved, [], 10),
        )
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = (
            ndcg_at_k(retrieved, binary, 10),
            ndcg_at_k(retrieved, graded, 5),
            ndcg_at_k(retrieved, {}, 10),
            ndcg_at_k(retrieved, [], 10),
        )
        assert all(math.isclose(a, e, rel_tol=1e-12) for a, e in zip(actual, expected, strict=True))

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        # IDS[:3] holds the single relevant judgment: recall is 1.0.
        assert metrics_mod.recall_at_k(IDS[:3], IDS[1:2], 10) == 1.0
