"""The Rust `dominant_century` must be byte-identical to the Python it replaces.

It is the only works seam left: hashing, markers, `parse_fuzzy_date` and
`scan_dates` lost the accelerator benchmark's keep gate and stay Python.
Every vector runs under both ``RE_RUST_BACKEND=rust`` and ``=python``.

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import pytest

from research_engine.services.text.dates import dominant_century


class TestDominantCenturyParity:
    @pytest.mark.parametrize(
        "text",
        [
            "1801 1802 1803 1804 1805 1901",
            "1801 1802 1901 1902 1903 2001",
            "1801 1802 1803 1804",
            "1801 1802 1901 1902 2001 2002",
            "London, March 24, 1862. Camp, June 3d 1863. Home, 1864, 1865, 1866.",
            "1499 1500 1599 1600 2099 2100",
            "",
            "no years at all",
        ],
    )
    def test_century_matches_across_backends(self, text, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = dominant_century(text)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert dominant_century(text) == expected

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert dominant_century("1801 1802 1803 1804 1805 1901") == 1800
