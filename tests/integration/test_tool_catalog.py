"""WI-5: installing a pack is visible on the wire without a restart.

Builds a container, lists tools, loads the history pack, and lists again:
the count grows by 2, both new ids are callable through handle_call_tool,
and find_passages' schema is rebuilt from the registry.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from research_engine.mcp.dispatch import refresh_pack_tools, register_core_tools
from research_engine.mcp.tools import find_passages
from research_engine.plugins.discovery import scan_plugins
from research_engine.plugins.loader import PluginLoader
from research_engine.plugins.registry import PluginRegistry

pytestmark = pytest.mark.integration



class _StubServer:
    """Captures the handlers `register_core_tools` gives the MCP server."""

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


class _FakeEventClient:
    """An event service holding no letters: cadence reports nothing to check."""

    async def query(self, *args, **kwargs):
        return [], []


def _history():
    return next(plugin for plugin in scan_plugins().plugins if plugin.plugin_id == "history")


def _wired_container(tmp_path):
    registry = PluginRegistry()
    loader = PluginLoader(
        AsyncMock(),
        registry,
        tmp_path / "plugin-data",
        event=_FakeEventClient(),
    )
    container = SimpleNamespace(registry=registry, plugin_loader=loader)
    return container, loader, registry


async def _list_ids(server) -> dict[str, dict]:
    tools = await server.list_fn()
    return {t.name: t for t in tools}


async def test_install_without_restart(tmp_path):
    container, loader, registry = _wired_container(tmp_path)
    server = _StubServer()
    register_core_tools(server, container)

    before = await _list_ids(server)
    assert "history.find_missing_letters" not in before

    # A pack contributing a filter extension changes a *core* tool's
    # signature; history itself contributes none, so a synthetic extension
    # proves the rebuild path refresh must take.
    registry.register_filter_extension(
        "synth_ext",
        SimpleNamespace(description="synthetic", input_schema={"type": "object"}),
        "test",
    )

    await loader._load_one(_history())
    refresh_pack_tools(container)

    after = await _list_ids(server)
    assert len(after) == len(before) + 2
    assert "history.find_missing_letters" in after
    assert "history.correspondence_cadence" in after

    # The new ids are callable through the wire handler, not just listed.
    a = "11111111-1111-1111-1111-111111111111"
    b = "22222222-2222-2222-2222-222222222222"
    [msg] = await server.call_fn(
        "history.find_missing_letters",
        {"correspondent_a_entity_id": a,
         "correspondent_b_entity_id": b,
         "method": "cadence"},
    )
    body = json.loads(msg["text"])
    assert "error" not in body
    assert body["candidates"] == []

    # ...and the fresh schemas actually validate.
    [msg] = await server.call_fn("history.find_missing_letters", {})
    body = json.loads(msg["text"])
    assert body["error"]["code"] == "validation_error"
    assert "correspondent_a_entity_id" in body["error"]["message"]

    # find_passages picked up the extension the registry gained.
    schema = after["find_passages"].inputSchema
    assert schema == find_passages.build_dynamic_schema(registry)
    assert "synth_ext" in schema["properties"]["filters"]["properties"]["extensions"]["properties"]


