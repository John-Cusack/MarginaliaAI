"""The installed pack is discoverable, approvable and loads its contributions."""

from __future__ import annotations

import pytest

from research_engine.domain.provenance import PluginActivationState
from research_engine.plugins.activation import PluginActivationManager
from research_engine.plugins.discovery import scan_plugins
from research_engine.plugins.loader import PluginLoader
from research_engine.plugins.registry import PluginRegistry
from research_engine_sdk import parse_manifest

pytestmark = pytest.mark.unit

TOOLS = {
    "dilemma.profile_load",
    "dilemma.profile_show",
    "dilemma.ingest",
    "dilemma.coverage",
    "dilemma.blocked_queue",
}


class MemoryRepo:
    def __init__(self) -> None:
        self.row = None

    async def save(self, activation):
        self.row = activation

    async def get(self, plugin_id):
        return self.row if self.row and self.row.plugin_id == plugin_id else None

    async def list_all(self):
        return [self.row] if self.row else []

    async def list_enabled(self):
        return [self.row] if self.row and self.row.enabled else []

    async def update_state(self, plugin_id, state, *, enabled=None, last_error=None,
                           last_seen_at=None):
        self.row = self.row.model_copy(
            update={
                "state": state,
                "enabled": self.row.enabled if enabled is None else enabled,
                "last_error": last_error,
                "last_seen_at": last_seen_at or self.row.last_seen_at,
            }
        )

    async def record_migration(self, *args, **kwargs):
        raise AssertionError("dilemma declares no database migration")

    async def delete(self, plugin_id):
        self.row = None


def _discovery():
    matches = [p for p in scan_plugins().plugins if p.plugin_id == "dilemma"]
    assert len(matches) == 1
    return matches[0]


def test_manifest_resources_and_tool_schemas_are_complete() -> None:
    plugin = _discovery()
    manifest = parse_manifest(plugin.manifest_path)

    assert {tool.id for tool in manifest.provides.mcp_tools} == TOOLS
    assert all(t.input_schema.get("type") == "object" for t in manifest.provides.mcp_tools)
    assert manifest.provides.database is None, "the pack owns no tables"
    assert manifest.permissions.ingest
    assert not manifest.permissions.llm


async def test_approved_pack_loads_its_tools_and_types(tmp_path) -> None:
    plugin = _discovery()
    repository = MemoryRepo()
    manager = PluginActivationManager(repository)
    [available] = await manager.inventory(discovered_plugins=[plugin])
    assert available.state is PluginActivationState.available
    await manager.approve("dilemma", non_interactive=True, discovered_plugins=[plugin])

    registry = PluginRegistry()
    registry.register_core_types()
    loader = PluginLoader(repository, registry, tmp_path / "plugin-data")

    assert await loader.load_enabled([plugin]) == ["dilemma"]
    assert set(registry.get_mcp_tools()) == TOOLS
    types = registry.list_document_types()
    assert {"versified_text", "source_document"} <= set(types)
    # Laid out by the SDK's structured importer, re-chunked by core's chunker.
    assert {types[t]["default_chunker"] for t in ("versified_text", "source_document")} == {
        "unit_window"
    }
