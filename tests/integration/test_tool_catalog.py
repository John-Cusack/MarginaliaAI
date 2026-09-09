"""WI-5: installing a pack is visible on the wire without a restart.

Builds a container, lists tools, loads the history pack, and lists again:
the count grows by 2, both new ids are callable through handle_call_tool,
and find_passages' schema is rebuilt from the registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from research_engine.mcp.dispatch import refresh_pack_tools, register_core_tools
from research_engine.mcp.tools import find_passages
from research_engine.plugins.loader import PluginLoader
from research_engine.plugins.manifest import parse_manifest
from research_engine.plugins.registry import PluginRegistry

pytestmark = pytest.mark.integration

HISTORY_PACK_DIR = (
    Path(__file__).resolve().parents[2] / "packages" / "plugins" / "history"
)


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
        return [], None


def _wired_container():
    registry = PluginRegistry()
    loader = PluginLoader(
        AsyncMock(), registry, HISTORY_PACK_DIR.parent,
        event_service=_FakeEventClient(),
    )
    container = SimpleNamespace(registry=registry, plugin_loader=loader)
    return container, loader, registry


async def _list_ids(server) -> dict[str, dict]:
    tools = await server.list_fn()
    return {t.name: t for t in tools}


async def test_install_without_restart():
    container, loader, registry = _wired_container()
    server = _StubServer()
    register_core_tools(server, container)

    before = await _list_ids(server)
    assert len(before) == 39
    assert "history.find_missing_letters" not in before

    # A pack contributing a filter extension changes a *core* tool's
    # signature; history itself contributes none, so a synthetic extension
    # proves the rebuild path refresh must take.
    registry.register_filter_extension(
        "synth_ext",
        SimpleNamespace(description="synthetic", input_schema={"type": "object"}),
        "test",
    )

    manifest = parse_manifest(HISTORY_PACK_DIR / "pack.yaml")
    await loader._load_one(manifest, HISTORY_PACK_DIR)
    refresh_pack_tools(container)

    after = await _list_ids(server)
    assert len(after) == 41
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


async def test_unload_is_expressible():
    """Replacing the pack slice with the empty set unlists every pack tool."""
    container, loader, registry = _wired_container()
    server = _StubServer()
    register_core_tools(server, container)

    manifest = parse_manifest(HISTORY_PACK_DIR / "pack.yaml")
    await loader._load_one(manifest, HISTORY_PACK_DIR)
    refresh_pack_tools(container)
    assert len(await _list_ids(server)) == 41

    # Unload: the registry forgets the pack, the catalogue follows.
    for tool_id in [t for t in registry.get_mcp_tools()]:
        del registry._mcp_tools[tool_id]
    refresh_pack_tools(container)

    after = await _list_ids(server)
    assert len(after) == 39
    assert "history.find_missing_letters" not in after

    [msg] = await server.call_fn("history.find_missing_letters", {})
    assert json.loads(msg["text"])["error"]["code"] == "unknown_tool"
