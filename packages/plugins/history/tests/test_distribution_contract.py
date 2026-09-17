from __future__ import annotations

from history.tools.correspondence_cadence import tool_handler

from research_engine.domain.provenance import PluginActivationState
from research_engine.plugins.activation import PluginActivationManager
from research_engine.plugins.discovery import scan_plugins
from research_engine.plugins.loader import PluginLoader
from research_engine.plugins.registry import PluginRegistry
from research_engine_sdk import EventFilter, parse_manifest


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

    async def update_state(
        self,
        plugin_id,
        state,
        *,
        enabled=None,
        last_error=None,
        last_seen_at=None,
    ):
        self.row = self.row.model_copy(
            update={
                "state": state,
                "enabled": self.row.enabled if enabled is None else enabled,
                "last_error": last_error,
                "last_seen_at": last_seen_at or self.row.last_seen_at,
            }
        )

    async def record_migration(self, *args, **kwargs):
        raise AssertionError("history declares no database migration")

    async def delete(self, plugin_id):
        self.row = None


def _history_discovery():
    matches = [
        plugin for plugin in scan_plugins().plugins if plugin.plugin_id == "history"
    ]
    assert len(matches) == 1
    return matches[0]


def test_manifest_resources_and_tool_schemas_are_complete() -> None:
    plugin = _history_discovery()
    manifest = parse_manifest(plugin.manifest_path)

    assert manifest.schema_version == 2
    assert {tool.id for tool in manifest.provides.mcp_tools} == {
        "history.find_missing_letters",
        "history.correspondence_cadence",
    }
    assert all(tool.input_schema.get("type") == "object" for tool in manifest.provides.mcp_tools)
    assert all(plugin.resource_path(path).is_file() for path in manifest.resource_paths())


async def test_installed_history_available_enable_loads_both_tools(tmp_path) -> None:
    plugin = _history_discovery()
    repository = MemoryRepo()
    manager = PluginActivationManager(repository)

    [available] = await manager.inventory(discovered_plugins=[plugin])
    assert available.state is PluginActivationState.available
    await manager.approve(
        "history",
        non_interactive=True,
        discovered_plugins=[plugin],
    )
    registry = PluginRegistry()
    registry.register_core_types()
    loader = PluginLoader(repository, registry, tmp_path / "plugin-data")

    assert await loader.load_enabled([plugin]) == ["history"]
    assert set(registry.get_mcp_tools()) == {
        "history.find_missing_letters",
        "history.correspondence_cadence",
    }


class EventClient:
    def __init__(self) -> None:
        self.filter = None

    async def query(self, filters, k=1000, group_by=None):
        self.filter = filters
        return [], []


async def test_cadence_queries_with_sdk_event_filter() -> None:
    events = EventClient()

    result = await tool_handler(
        events,
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    )

    assert isinstance(events.filter, EventFilter)
    assert result["summary"]["total_letters"] == 0
