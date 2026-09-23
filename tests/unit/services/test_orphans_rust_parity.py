"""The Rust report-math seam must match the reindex reports.

`Orphan.dependent_total` sums one orphan's table counts;
`ReindexReport.orphaned_dependents` sums a run's; `orphan_rate` pins the
fraction bit-exact (exactly-rounded division both sides, vacuous 1.0
... here 0.0 on an empty run); `exceeded` pins the threshold decision,
including the boundary-adjacent 0.005 case.

Rust-forced cases skip when the accelerator wheel is absent; the Python
path always runs.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from research_engine.services.ingestion import reindex as reindex_mod
from research_engine.services.ingestion.reindex import Orphan, ReindexReport


def _orphan(dependents: dict[str, int]) -> Orphan:
    return Orphan(
        document_id=UUID(int=0),
        passage_id=UUID(int=1),
        text_preview="t",
        dependents=dependents,
        reason="r",
    )


def _report(before: int, orphans: list[Orphan]) -> ReindexReport:
    return ReindexReport(passages_before=before, orphans=orphans)


class TestOrphanMathParity:
    @pytest.mark.parametrize(
        "dependents",
        [
            {},
            {"mentions": 2},
            {"mentions": 2, "extractions": 3, "events": 0},
        ],
    )
    def test_dependent_totals_match(self, dependents, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert _orphan(dependents).dependent_total == sum(dependents.values())
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _orphan(dependents).dependent_total == sum(dependents.values())

    def test_run_sums_match(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        orphans = [_orphan({"a": 2}), _orphan({}), _orphan({"b": 1, "c": 4})]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert _report(100, orphans).orphaned_dependents == 7
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _report(100, orphans).orphaned_dependents == 7
        assert _report(100, []).orphaned_dependents == 0


class TestOrphanRateParity:
    @pytest.mark.parametrize(
        ("before", "count"),
        [
            (0, 0),
            (200, 0),
            (200, 1),
            (200, 2),
            (3, 1),
        ],
    )
    def test_rates_match_bit_exact(self, before, count, monkeypatch):
        pytest.importorskip("marginalia_rs")
        orphans = [_orphan({}) for _ in range(count)]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _report(before, orphans).orphan_rate
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = _report(before, orphans).orphan_rate
        assert actual.hex() == expected.hex()

    @pytest.mark.parametrize(
        ("before", "count", "threshold", "expected"),
        [
            (0, 0, 0.005, False),
            (200, 1, 0.005, False),
            (200, 2, 0.005, True),
            (200, 0, 0.0, False),
            (200, 1, 0.0, True),
        ],
    )
    def test_threshold_decisions_match(self, before, count, threshold, expected, monkeypatch):
        pytest.importorskip("marginalia_rs")
        orphans = [_orphan({}) for _ in range(count)]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert _report(before, orphans).exceeded(threshold) is expected
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _report(before, orphans).exceeded(threshold) is expected

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert reindex_mod.ReindexReport is ReindexReport
        assert reindex_mod.Orphan is Orphan
        assert _report(0, []).orphan_rate == 0.0
        assert _report(0, []).exceeded(0.0) is False
