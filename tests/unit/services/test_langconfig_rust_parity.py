"""The Rust langconfig seam must match the Python table exactly.

Every vector runs under both ``RE_RUST_BACKEND=rust`` and ``=python``.
The lookup is a pure table with a ``simple`` fallback — no crossing
concerns — so this suite mostly pins the.compiler: locales, case,
whitespace, unknowns, and the constant surface.

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import pytest

from research_engine.services.search import langconfig as langconfig_py
from research_engine.services.search.hybrid import _pg_config

ISOS = [None, "", "  ", "en", "EN", " en ", "de-CH", "pt-BR", " EL ", "xx", "e", "eng", "yiddish", "सर"]


class TestLangconfigParity:
    @pytest.mark.parametrize("iso", ISOS)
    def test_rust_matches_python_config(self, iso, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _pg_config(iso)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = _pg_config(iso)
        assert actual == expected == langconfig_py.pg_config(iso)

    def test_known_configs_match_as_sets(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        import marginalia_rs

        assert isinstance(marginalia_rs.chunk.KNOWN_CONFIGS, frozenset)
        assert set(marginalia_rs.chunk.KNOWN_CONFIGS) == set(langconfig_py.KNOWN_CONFIGS)
        assert marginalia_rs.chunk.DEFAULT_CONFIG == langconfig_py.DEFAULT_CONFIG
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert all(langconfig_py.is_known_config(c) for c in langconfig_py.KNOWN_CONFIGS)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        from research_engine.adapters.storage.postgres.repositories.passages import (
            _is_known_config,
        )

        assert all(_is_known_config(c) for c in langconfig_py.KNOWN_CONFIGS)
        assert not _is_known_config("english; DROP TABLE x")
        assert not _is_known_config("")

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert _pg_config("de-CH") == "german"
        assert _pg_config(None) == "simple"
