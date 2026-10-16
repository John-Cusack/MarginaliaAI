"""A deployed reference server lists and answers only its read-only tools.

The bug this guards: every core tool — the writers, the LLM-spending
`extract`, and the `ingest_execute` trampoline — was listed and callable on
every server, so a corpus served to other agents was also open to edit.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from research_engine.config.settings import Settings
from research_engine.mcp import dispatch
from research_engine.mcp.dispatch import dispatch_tool, register_core_tools
from research_engine.mcp.profiles import REFERENCE_TOOLS, resolve_tool_allowlist
from research_engine.mcp.tools import ingest_execute

pytestmark = pytest.mark.unit


class _StubServer:
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


def _wire(settings: Settings | None, plugin_tools: dict[str, Any] | None = None):
    from research_engine.plugins.registry import PluginRegistry

    server = _StubServer()
    registry = PluginRegistry()
    for tool_id, handler in (plugin_tools or {}).items():
        registry.register_mcp_tool(tool_id, handler, "test_pack")
    registry.get_filter_extensions = lambda: {}  # type: ignore[method-assign]
    container = SimpleNamespace(
        registry=registry, plugin_loader=None, settings=settings
    )
    register_core_tools(server, container)
    return server, container


async def _listed(server: _StubServer) -> set[str]:
    return {tool.name for tool in await server.list_fn()}


async def _pack_tool(**_kwargs: Any) -> dict[str, Any]:
    return {"ok": True}


def test_every_reference_tool_is_a_core_tool() -> None:
    assert set(dispatch._CORE_TOOL_MAP) >= REFERENCE_TOOLS  # noqa: SLF001


def test_reference_profile_leaves_out_writers_and_spenders() -> None:
    for name in ("extract", "ingest_execute", "work_publish", "upsert_entity",
                 "claim_upsert", "upsert_edge"):
        assert name not in REFERENCE_TOOLS


async def test_no_profile_lists_everything() -> None:
    server, _ = _wire(Settings(), {"test_pack.tool": _pack_tool})
    listed = await _listed(server)
    assert set(dispatch._CORE_TOOL_MAP) <= listed  # noqa: SLF001
    assert "test_pack.tool" in listed


async def test_reference_profile_lists_only_reference_tools() -> None:
    server, _ = _wire(
        Settings(mcp_profile="reference"), {"test_pack.tool": _pack_tool}
    )
    assert await _listed(server) == REFERENCE_TOOLS


async def test_call_outside_the_profile_is_an_unknown_tool() -> None:
    server, _ = _wire(
        Settings(mcp_profile="reference"), {"test_pack.tool": _pack_tool}
    )
    for name in ("extract", "work_publish", "test_pack.tool"):
        [msg] = await server.call_fn(name, {})
        assert json.loads(msg["text"])["error"]["code"] == "unknown_tool"


async def test_tool_list_alone_is_an_allowlist() -> None:
    server, _ = _wire(Settings(mcp_tools="find_passages, test_pack.tool"),
                      {"test_pack.tool": _pack_tool})
    assert await _listed(server) == {"find_passages", "test_pack.tool"}
    [msg] = await server.call_fn("test_pack.tool", {})
    assert json.loads(msg["text"]) == {"ok": True}


def test_tool_list_narrows_a_profile_and_cannot_widen_it() -> None:
    allowed = resolve_tool_allowlist(
        Settings(mcp_profile="reference", mcp_tools="find_passages,extract")
    )
    assert allowed == frozenset({"find_passages"})


def test_unset_means_no_restriction() -> None:
    assert resolve_tool_allowlist(Settings()) is None
    assert resolve_tool_allowlist(None) is None


@pytest.mark.parametrize("blank", ["", "  ", " , "])
def test_blank_tool_list_means_unset(blank: str) -> None:
    """A placeholder `RE_MCP_TOOLS=` must not empty the surface or arm the guard."""
    assert resolve_tool_allowlist(Settings(mcp_tools=blank)) is None
    assert not Settings(mcp_tools=blank).restricts_mcp_surface
    assert (
        resolve_tool_allowlist(Settings(mcp_profile="reference", mcp_tools=blank))
        == REFERENCE_TOOLS
    )


async def test_dispatch_tool_refuses_outside_the_profile() -> None:
    _, container = _wire(Settings(mcp_profile="reference"))
    with pytest.raises(PermissionError, match="extract"):
        await dispatch_tool(container, "extract", {})


async def test_ingest_execute_cannot_trampoline_past_the_profile() -> None:
    """Even called directly, the trampoline is held to the listed surface."""
    _, container = _wire(Settings(mcp_tools="ingest_execute,find_passages"))
    result = await ingest_execute.handler(
        container, actions=[{"tool": "work_publish", "args": {}}]
    )
    [entry] = result["results"]
    assert entry["status"] == "failed"
    assert "not exposed" in entry["error"]
