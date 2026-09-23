"""The Rust predicate seam must match the ingestion callers.

`_output_is_identical` compares spans and text only — version, token
estimates, and metadata are labels, verified here by objects that differ
in everything but the triple. `CoverageReport.complete`/`coverage` pin
the report math bit-exact (exactly-rounded division both sides).
`get_chunker` refuses unknown ids with the verbatim text.

Rust-forced cases skip when the accelerator wheel is absent; the Python
path always runs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from research_engine.services.ingestion import embedding_backfill as backfill_mod
from research_engine.services.ingestion import pipeline as pipeline_mod
from research_engine.services.ingestion import reindex as reindex_mod
from research_engine.services.ingestion.embedding_backfill import CoverageReport
from research_engine.services.ingestion.pipeline import get_chunker
from research_engine.services.ingestion.reindex import _output_is_identical


def _passage(start: int, end: int, text: str, **labels: object) -> SimpleNamespace:
    return SimpleNamespace(char_start=start, char_end=end, text=text, **labels)


def _report(missing: int, wrong: int, embedded: int, total: int) -> CoverageReport:
    return CoverageReport(
        model="m",
        model_version="1",
        dim=8,
        total_passages=total,
        embedded=embedded,
        missing=missing,
        wrong_dimension=wrong,
    )


class TestOutputIdenticalParity:
    @pytest.mark.parametrize(
        ("old", "new", "expected"),
        [
            ([], [], True),
            (
                [_passage(0, 5, "hi")],
                [_passage(0, 5, "hi")],
                True,
            ),
            (
                [_passage(0, 5, "hi")],
                [_passage(0, 5, "hi"), _passage(5, 9, "yo")],
                False,
            ),
            (
                [_passage(0, 5, "hi")],
                [_passage(1, 5, "hi")],
                False,
            ),
            (
                [_passage(0, 5, "hi")],
                [_passage(0, 5, "yo")],
                False,
            ),
            # Labels are not content.
            (
                [_passage(0, 5, "hi", chunker_version="1.0", metadata={"a": 1})],
                [_passage(0, 5, "hi", chunker_version="9.9", metadata={})],
                True,
            ),
        ],
    )
    def test_identity_matches(self, old, new, expected, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert _output_is_identical(old, new) is expected
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _output_is_identical(old, new) is expected


class TestCoverageMathParity:
    @pytest.mark.parametrize(
        ("missing", "wrong", "embedded", "total"),
        [
            (0, 0, 0, 0),
            (0, 0, 7, 10),
            (3, 0, 7, 10),
            (0, 2, 7, 10),
            (0, 0, 10, 10),
        ],
    )
    def test_report_math_matches_bit_exact(self, missing, wrong, embedded, total, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        py_report = _report(missing, wrong, embedded, total)
        py_complete, py_coverage = py_report.complete, py_report.coverage
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        rs_report = _report(missing, wrong, embedded, total)
        assert rs_report.complete is py_complete
        assert rs_report.coverage.hex() == py_coverage.hex()

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert backfill_mod.CoverageReport is CoverageReport
        assert reindex_mod._output_is_identical is _output_is_identical
        assert pipeline_mod.get_chunker is get_chunker
        assert _report(0, 0, 0, 0).coverage == 1.0


class TestChunkerMessageParity:
    def test_unknown_chunker_refused_verbatim(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(ValueError) as py_exc:
            get_chunker("nope")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ValueError) as rs_exc:
            get_chunker("nope")
        assert str(rs_exc.value) == str(py_exc.value) == "Unknown chunker: nope"

    def test_known_chunkers_resolve(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert get_chunker("prose_window") is not None
