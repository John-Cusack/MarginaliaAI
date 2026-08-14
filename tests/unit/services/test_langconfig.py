"""Language-config resolution and the indexed keyword-search SQL."""

from __future__ import annotations

import pytest

from research_engine.adapters.storage.postgres.repositories.passages import (
    build_keyword_search_sql,
)
from research_engine.services.search.langconfig import (
    DEFAULT_CONFIG,
    is_known_config,
    pg_config,
)

pytestmark = pytest.mark.unit


# --- Mapping ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("iso", "expected"),
    [
        ("en", "english"),
        ("de", "german"),
        ("fr", "french"),
        ("ru", "russian"),
        ("DE", "german"),
        ("de-CH", "german"),
        ("de_AT", "german"),
        ("  fr  ", "french"),
    ],
)
def test_known_languages_map_to_their_stemmer(iso: str, expected: str) -> None:
    assert pg_config(iso) == expected


@pytest.mark.parametrize("iso", [None, "", "xx", "klingon", "zz-ZZ"])
def test_unknown_language_falls_back_to_simple_not_english(iso: str | None) -> None:
    """`simple` degrades gracefully; `english` degrades wrongly and invisibly."""
    assert pg_config(iso) == DEFAULT_CONFIG
    assert pg_config(iso) != "english"


def test_is_known_config_guards_sql_interpolation() -> None:
    assert is_known_config("german")
    assert is_known_config("simple")
    assert not is_known_config("english'; DROP TABLE core.passages; --")
    assert not is_known_config("klingon")


# --- Keyword search SQL ----------------------------------------------------


def test_single_config_produces_one_indexable_branch() -> None:
    sql = build_keyword_search_sql(["german"])
    assert sql.count("UNION ALL") == 0
    assert "plainto_tsquery('german', :query)" in sql
    assert "pf.lang_config = 'german'::regconfig" in sql


def test_multiple_configs_are_unioned() -> None:
    sql = build_keyword_search_sql(["english", "german", "simple"])
    assert sql.count("UNION ALL") == 2
    for cfg in ("english", "german", "simple"):
        assert f"plainto_tsquery('{cfg}', :query)" in sql
        assert f"pf.lang_config = '{cfg}'::regconfig" in sql


def test_tsquery_is_constant_within_each_branch() -> None:
    """The per-row form `plainto_tsquery(pf.lang_config, ...)` is correct and
    unusable: it varies per row, so the GIN index cannot be used and every
    search becomes a sequential scan. Guard against reintroducing it.
    """
    sql = build_keyword_search_sql(["english", "german"])
    assert "plainto_tsquery(pf.lang_config" not in sql
    assert "plainto_tsquery(pf." not in sql


def test_ordering_and_limit_apply_across_the_union() -> None:
    sql = build_keyword_search_sql(["english", "german"])
    assert sql.rstrip().endswith("LIMIT :k")
    assert sql.count("ORDER BY kw_score DESC") == 1


def test_candidate_filter_applies_in_every_branch() -> None:
    sql = build_keyword_search_sql(["english", "german"])
    assert sql.count(":no_filter OR pf.passage_id = ANY(:candidate_ids)") == 2


def test_unvalidated_config_is_refused() -> None:
    with pytest.raises(ValueError, match="unvalidated regconfig"):
        build_keyword_search_sql(["english'); DROP TABLE core.passages; --"])


def test_empty_config_list_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one config"):
        build_keyword_search_sql([])
