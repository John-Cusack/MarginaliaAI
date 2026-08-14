"""Filter translation in the passage repository.

The load-bearing test here is ``test_every_search_filter_field_is_supported``:
without it, adding a field to ``SearchFilters`` silently produces a filter that
is accepted, reported as applied, and never runs.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from research_engine.adapters.storage.postgres.repositories.passages import (
    SUPPORTED_FILTERS,
    build_candidate_stmt,
    validate_filters,
)
from research_engine.domain.errors import UnknownFilterExtension, UnsupportedFilterError
from research_engine.domain.passages import SearchFilters

pytestmark = pytest.mark.unit


def compiled(stmt: sa.Select) -> str:
    """Compile against the real dialect.

    Not the default dialect: `metadata.contains()` compiled to a string LIKE
    under both, but only the Postgres rendering shows whether the JSONB cast is
    present. Assertions about generated SQL are only worth making against the
    dialect that will actually run it.
    """
    return str(stmt.compile(dialect=postgresql.dialect()))


def compiled_with_values(stmt: sa.Select) -> str:
    """Compile with bound values inlined, for assertions about the values."""
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


class _FakeExtension:
    filter_id = "has_extraction"

    def build_clause(self, value: object) -> sa.Select:
        from research_engine.adapters.storage.postgres.schema import passages

        return sa.select(passages.c.id).where(passages.c.text == str(value))


# --- The reflection test ---------------------------------------------------


def test_every_search_filter_field_is_supported() -> None:
    """Every field of SearchFilters must have a branch in the repository.

    If this fails you have added a filter the repository cannot honour. Either
    implement the branch or remove the field — do not add it to
    SUPPORTED_FILTERS to make the test pass.
    """
    declared = set(SearchFilters.model_fields)
    missing = declared - SUPPORTED_FILTERS
    assert not missing, f"SearchFilters fields with no repository branch: {sorted(missing)}"


def test_supported_filters_has_no_dead_keys() -> None:
    """And nothing in SUPPORTED_FILTERS that SearchFilters cannot express."""
    dead = SUPPORTED_FILTERS - set(SearchFilters.model_fields)
    assert not dead, f"SUPPORTED_FILTERS keys not in SearchFilters: {sorted(dead)}"


# --- Fail-loud validation --------------------------------------------------


def test_unknown_filter_key_raises() -> None:
    with pytest.raises(UnsupportedFilterError) as exc:
        validate_filters({"document_types": ["letter"], "published_after": "1861"})
    assert exc.value.unknown == ["published_after"]
    assert "document_types" in exc.value.supported


def test_unsupported_filter_error_is_a_value_error() -> None:
    """MCP tools catch ValueError and report invalid_input; keep that path."""
    assert issubclass(UnsupportedFilterError, ValueError)
    assert issubclass(UnknownFilterExtension, ValueError)


def test_known_keys_pass_validation() -> None:
    validate_filters({key: None for key in SUPPORTED_FILTERS})


def test_unregistered_extension_raises() -> None:
    with pytest.raises(UnknownFilterExtension) as exc:
        validate_filters(
            {"extensions": {"event_date_range": {"start": "1861"}}},
            {"has_extraction": _FakeExtension()},
        )
    assert exc.value.extension_id == "event_date_range"
    assert exc.value.available == ["has_extraction"]


def test_extension_requested_with_no_registry_raises() -> None:
    """The old code silently dropped these — similar_to and extract pass no registry."""
    with pytest.raises(UnknownFilterExtension):
        validate_filters({"extensions": {"has_extraction": True}}, None)


def test_empty_extensions_is_not_an_error() -> None:
    validate_filters({"extensions": {}}, None)


# --- Translation -----------------------------------------------------------


def test_no_filters_produces_no_where_clause() -> None:
    sql = compiled(build_candidate_stmt({}))
    assert "WHERE" not in sql
    assert "JOIN" not in sql


def test_language_narrows_on_documents() -> None:
    sql = compiled_with_values(build_candidate_stmt({"language": "de"}))
    assert "JOIN core.documents" in sql
    assert "core.documents.language = 'de'" in sql


def test_document_types_and_language_join_documents_once() -> None:
    sql = compiled(build_candidate_stmt({"document_types": ["letter"], "language": "de"}))
    assert sql.count("JOIN core.documents") == 1


def test_author_names_match_document_metadata() -> None:
    sql = compiled_with_values(build_candidate_stmt({}, author_names=["Karl Barth", "Barth, K."]))
    assert "JOIN core.documents" in sql
    assert "core.documents.metadata ->> 'author'" in sql
    assert "karl barth" in sql.lower()
    assert "barth, k." in sql.lower()


def test_recipient_names_match_recipient_key() -> None:
    sql = compiled_with_values(build_candidate_stmt({}, recipient_names=["Thurneysen"]))
    assert "core.documents.metadata ->> 'recipient'" in sql


def test_metadata_filter_uses_jsonb_containment_not_string_like() -> None:
    """The generic JSON type compiles `.contains()` to a LIKE, which matches nothing.

    Guarding the cast explicitly: without it this filter is accepted, reported
    as applied, and silently returns the wrong rows.
    """
    sql = compiled(build_candidate_stmt({"metadata": {"page": 3}}))
    assert "@>" in sql
    assert "LIKE" not in sql


def test_author_entity_with_no_names_matches_nothing() -> None:
    """An entity with no canonical name or aliases must not silently match all."""
    sql = compiled(build_candidate_stmt({}, author_names=[]))
    assert "false" in sql.lower()


def test_mentions_entity_ids_add_one_subquery_each() -> None:
    ids = [UUID(int=1), UUID(int=2)]
    sql = compiled(build_candidate_stmt({"mentions_entity_ids": ids}))
    assert sql.count("FROM core.mentions") == 2


def test_extension_and_logic_intersects() -> None:
    stmt = build_candidate_stmt(
        {"extensions": {"has_extraction": "x"}, "extension_logic": "and"},
        {"has_extraction": _FakeExtension()},
    )
    assert "IN (SELECT" in compiled(stmt)


def test_extension_or_logic_unions() -> None:
    stmt = build_candidate_stmt(
        {"extensions": {"has_extraction": "x"}, "extension_logic": "or"},
        {"has_extraction": _FakeExtension()},
    )
    assert "IN (SELECT" in compiled(stmt)
