"""Tests for MCP dispatch helpers: client selection and input validation."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from research_engine.mcp.dispatch import (
    _select_clients,
    _validate_input,
    register_core_tools,
)

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


class _StubServer:
    """Captures the handlers `register_core_tools` would give the MCP server."""

    def __init__(self) -> None:
        self.list_fn = None
        self.call_fn = None

    def list_tools(self):
        def deco(fn):
            self.list_fn = fn
            return fn

        return deco

    def call_tool(self):
        def deco(fn):
            self.call_fn = fn
            return fn

        return deco


def _wire(plugin_tools: dict[str, Any], plugin_loader: Any = None) -> _StubServer:
    """Register core + the given pack tools against stub container wiring."""
    from research_engine.plugins.registry import PluginRegistry

    server = _StubServer()
    registry = PluginRegistry()
    for tool_id, handler in plugin_tools.items():
        registry.register_mcp_tool(tool_id, handler, "test_pack")
    registry.get_filter_extensions = lambda: {}  # type: ignore[method-assign]
    container = SimpleNamespace(registry=registry, plugin_loader=plugin_loader)
    register_core_tools(server, container)
    return server


class TestPermissionDenied:
    """WI-2: a denial must say so, not look like a crash."""

    async def test_pack_handler_denial_returns_permission_denied(self) -> None:
        from research_engine.plugins.permissions import DeniedLLMClient

        async def needs_llm(llm, *, query: str = "hi"):  # noqa: ANN001
            return await llm.complete(query)

        loader = SimpleNamespace(
            build_plugin_clients=lambda name: {"llm": DeniedLLMClient("test_pack")}
        )
        server = _wire({"test_pack.tool": needs_llm}, loader)

        [msg] = await server.call_fn("test_pack.tool", {"query": "hi"})
        body = json.loads(msg["text"])
        assert body["error"]["code"] == "permission_denied"
        assert body["error"]["details"] == {"plugin": "test_pack", "permission": "llm"}
        assert "pack.yaml" in body["error"]["message"]

    async def test_core_handler_denial_returns_permission_denied(
        self, monkeypatch
    ) -> None:
        from research_engine.domain.errors import PermissionDenied
        from research_engine.mcp.tools import list_filters

        async def boom(container, **kwargs):  # noqa: ANN001, ANN002
            raise PermissionDenied("test_pack", "network")

        monkeypatch.setattr(list_filters, "handler", boom)
        server = _wire({})

        [msg] = await server.call_fn("list_available_filters", {})
        body = json.loads(msg["text"])
        assert body["error"]["code"] == "permission_denied"
        assert body["error"]["details"] == {
            "plugin": "test_pack",
            "permission": "network",
        }

    async def test_ordinary_crash_still_reports_tool_failed(self) -> None:
        async def boom(**kwargs):  # noqa: ANN002
            raise RuntimeError("ordinary boom")

        server = _wire({"test_pack.tool": boom})

        [msg] = await server.call_fn("test_pack.tool", {})
        body = json.loads(msg["text"])
        assert body["error"]["code"] == "test_pack.tool_failed"
