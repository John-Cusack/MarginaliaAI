"""Tests for MCP dispatch helpers: client selection and input validation."""

from __future__ import annotations

from typing import Any

from research_engine.mcp.dispatch import _select_clients, _validate_input

ALL_CLIENTS = {
    "corpus": "C",
    "entity": "EN",
    "event": "EV",
    "extraction": "EX",
    "llm": "L",
    "http": "H",
    "ingestion": "I",
    "edge": "ED",
}


class TestSelectClients:
    def test_passes_only_declared_clients(self) -> None:
        async def handler(corpus, extraction, entity, event, *, query):  # noqa: ANN001
            ...

        selected = _select_clients(handler, ALL_CLIENTS)
        assert set(selected) == {"corpus", "extraction", "entity", "event"}

    def test_single_client_subset(self) -> None:
        async def handler(corpus, *, query):  # noqa: ANN001
            ...

        selected = _select_clients(handler, ALL_CLIENTS)
        assert set(selected) == {"corpus"}

    def test_var_keyword_receives_all(self) -> None:
        async def handler(corpus=None, entity=None, **kwargs):  # noqa: ANN001
            ...

        selected = _select_clients(handler, ALL_CLIENTS)
        assert selected == ALL_CLIENTS

    def test_no_client_params(self) -> None:
        async def handler(*, query):  # noqa: ANN001
            ...

        assert _select_clients(handler, ALL_CLIENTS) == {}

    def test_empty_clients_short_circuits(self) -> None:
        async def handler(corpus):  # noqa: ANN001
            ...

        assert _select_clients(handler, {}) == {}

    def test_uninspectable_handler_passes_all(self) -> None:
        # If signature introspection fails, fall back to passing all clients
        # rather than dropping them. A non-callable raises TypeError.
        selected = _select_clients(42, ALL_CLIENTS)
        assert selected == ALL_CLIENTS


class TestValidateInput:
    SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
            "tags": {"type": "array"},
            "mode": {"type": "string", "enum": ["fast", "slow"]},
        },
        "required": ["query"],
    }

    def test_valid_passes(self) -> None:
        assert _validate_input(self.SCHEMA, {"query": "hi", "limit": 5, "mode": "fast"}) is None

    def test_missing_required(self) -> None:
        err = _validate_input(self.SCHEMA, {"limit": 5})
        assert err is not None and "query" in err

    def test_wrong_type_string_for_integer(self) -> None:
        err = _validate_input(self.SCHEMA, {"query": "hi", "limit": "5"})
        assert err is not None and "limit" in err

    def test_bool_rejected_for_integer(self) -> None:
        err = _validate_input(self.SCHEMA, {"query": "hi", "limit": True})
        assert err is not None and "limit" in err

    def test_number_accepts_int_and_float(self) -> None:
        assert _validate_input(self.SCHEMA, {"query": "hi", "ratio": 1}) is None
        assert _validate_input(self.SCHEMA, {"query": "hi", "ratio": 1.5}) is None

    def test_enum_out_of_range(self) -> None:
        err = _validate_input(self.SCHEMA, {"query": "hi", "mode": "medium"})
        assert err is not None and "mode" in err

    def test_unknown_field_ignored(self) -> None:
        # Fields without a property spec pass through (no schema to check).
        assert _validate_input(self.SCHEMA, {"query": "hi", "extra": 123}) is None
