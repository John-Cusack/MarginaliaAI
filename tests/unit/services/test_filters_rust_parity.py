"""The Rust filter-SQL seam must emit the Python SQL byte-for-byte.

`build_keyword_search_sql` interpolates validated regconfigs as literals
(index use demands it), so the seam pins the whole statement text, the
branch order, and both `ValueError` messages — including the Python list
rendering of refused configs. `like_escape` pins the chained replaces,
with hand-computed cases proving the backslash-first order rather than
both backends agreeing on the same mistake.

Rust-forced cases skip when the accelerator wheel is absent; the Python
path always runs.
"""

from __future__ import annotations

import pytest

from research_engine.adapters.storage.postgres.repositories import (
    document_texts as texts_mod,
)
from research_engine.adapters.storage.postgres.repositories.document_texts import (
    _like_escape,
)
from research_engine.adapters.storage.postgres.repositories.passages import (
    build_keyword_search_sql,
)


class TestKeywordSqlParity:
    @pytest.mark.parametrize(
        "configs",
        [
            ["english"],
            ["english", "german"],
            ["german", "english", "french"],
            ["english", "english"],
            ["arabic", "basque", "danish", "dutch", "finnish"],
        ],
    )
    def test_sql_matches_byte_for_byte(self, configs, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = build_keyword_search_sql(configs)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = build_keyword_search_sql(configs)
        assert actual == expected

    def test_branch_order_follows_config_order(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        sql = build_keyword_search_sql(["german", "english"])
        assert sql.index("'german'") < sql.index("'english'")
        assert "UNION ALL" in sql
        assert sql.endswith("\nORDER BY kw_score DESC\nLIMIT :k")

    def test_empty_configs_rejected(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(
            ValueError,
            match=r"^build_keyword_search_sql requires at least one config$",
        ):
            build_keyword_search_sql([])

    @pytest.mark.parametrize(
        ("configs", "rendered"),
        [
            (["xx"], "['xx']"),
            (["english", "xx;q", "yy"], "['xx;q', 'yy']"),
        ],
    )
    def test_unvalidated_configs_rejected(self, configs, rendered, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(ValueError) as py_exc:
            build_keyword_search_sql(configs)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ValueError) as rs_exc:
            build_keyword_search_sql(configs)
        assert str(rs_exc.value) == str(py_exc.value)
        assert str(rs_exc.value) == (
            f"refusing to interpolate unvalidated regconfig(s): {rendered}"
        )

    def test_tuple_configs_accepted(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = build_keyword_search_sql(["english"])
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert build_keyword_search_sql(("english",)) == expected


class TestLikeEscapeParity:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", ""),
            ("plain quote", "plain quote"),
            ("100% coverage", "100\\% coverage"),
            ("snake_case", "snake\\_case"),
            ("back\\slash", "back\\\\slash"),
            # Backslash first: the escape backslash itself is not re-escaped.
            ("\\%", "\\\\\\%"),
            ("%_\\", "\\%\\_\\\\"),
            ("hébreu 100%_שלום", "hébreu 100\\%\\_שלום"),
        ],
    )
    def test_escape_matches_hand_computed(self, value, expected, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert _like_escape(value) == expected
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _like_escape(value) == expected

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert texts_mod._like_escape("a%b_c\\d") == "a\\%b\\_c\\\\d"
