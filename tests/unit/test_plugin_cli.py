"""Plugin activation state machine and CLI contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from research_engine.cli.plugin import plugin_app
from research_engine.domain.provenance import (
    PluginActivation,
    PluginActivationState,
)
from research_engine.plugins.activation import (
    PluginActivationManager,
    PluginNotInstalledError,
    PluginStatus,
)
from research_engine.plugins.discovery import DiscoveredPlugin
from research_engine_sdk import PluginManifest

runner = CliRunner()


class MemoryActivationRepo:
    def __init__(self) -> None:
        self.rows: dict[str, PluginActivation] = {}

    async def save(self, activation: PluginActivation) -> None:
        self.rows[activation.plugin_id] = activation

    async def get(self, plugin_id: str) -> PluginActivation | None:
        return self.rows.get(plugin_id)

    async def list_all(self) -> list[PluginActivation]:
        return list(self.rows.values())

    async def list_enabled(self) -> list[PluginActivation]:
        return [row for row in self.rows.values() if row.enabled]

    async def update_state(
        self,
        plugin_id,
        state,
        *,
        enabled=None,
        last_error=None,
        last_seen_at=None,
    ) -> None:
        row = self.rows[plugin_id]
        self.rows[plugin_id] = row.model_copy(
            update={
                "state": state,
                "enabled": row.enabled if enabled is None else enabled,
                "last_error": last_error,
                "last_seen_at": last_seen_at or row.last_seen_at,
            }
        )

    async def delete(self, plugin_id: str) -> None:
        del self.rows[plugin_id]


def _discovered(tmp_path, *, version: str = "0.2.0", digest: str = "a" * 64):
    manifest = PluginManifest.model_validate(
        {
            "schema_version": 2,
            "plugin_id": "history",
            "requires": {"core_api": ">=0.6,<0.7"},
            "permissions": {"network": "none", "llm": True},
            "provides": {
                "document_types": [
                    {"id": "letter", "default_chunker": "whole_or_paragraph"}
                ],
                "mcp_tools": [
                    {
                        "id": "history.find",
                        "entry": "history.tools:handler",
                        "description": "Find history",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            },
        }
    )
    package = tmp_path / "history"
    package.mkdir(exist_ok=True)
    manifest_path = package / "plugin.yaml"
    manifest_path.write_text("schema_version: 2\\nplugin_id: history\\n")
    return DiscoveredPlugin(
        plugin_id="history",
        module_name="history",
        entry_point_name="history",
        distribution_name="research-engine-plugin-history",
        distribution_version=version,
        project_urls={"Source": "https://example.test/history"},
        direct_url=None,
        manifest=manifest,
        manifest_sha256=digest,
        manifest_bytes=manifest_path.read_bytes(),
        manifest_path=manifest_path,
        package_root=package,
    )


async def test_new_distribution_is_available_not_enabled(tmp_path) -> None:
    manager = PluginActivationManager(MemoryActivationRepo())

    [status] = await manager.inventory(discovered_plugins=[_discovered(tmp_path)])

    assert status.state is PluginActivationState.available
    assert status.activation is None


async def test_approval_records_exact_artifact_and_permissions(tmp_path) -> None:
    repository = MemoryActivationRepo()
    manager = PluginActivationManager(repository)
    discovered = _discovered(tmp_path)

    activation = await manager.approve(
        "history",
        non_interactive=True,
        discovered_plugins=[discovered],
    )

    assert activation.enabled is True
    assert activation.state is PluginActivationState.enabled
    assert activation.distribution_version == "0.2.0"
    assert activation.manifest_sha256 == "a" * 64
    assert activation.permissions_granted["llm"] is True
    assert activation.approved_non_interactive is True


async def test_changed_artifact_becomes_pending_approval(tmp_path) -> None:
    repository = MemoryActivationRepo()
    manager = PluginActivationManager(repository)
    original = _discovered(tmp_path)
    await manager.approve(
        "history", non_interactive=False, discovered_plugins=[original]
    )

    [status] = await manager.inventory(
        persist=True,
        discovered_plugins=[
            _discovered(tmp_path, version="0.2.1", digest="b" * 64)
        ],
    )

    assert status.state is PluginActivationState.pending_approval
    assert repository.rows["history"].state is PluginActivationState.pending_approval


async def test_removed_distribution_becomes_missing_without_deleting_audit(tmp_path) -> None:
    repository = MemoryActivationRepo()
    manager = PluginActivationManager(repository)
    await manager.approve(
        "history",
        non_interactive=False,
        discovered_plugins=[_discovered(tmp_path)],
    )

    [status] = await manager.inventory(persist=True, discovered_plugins=[])

    assert status.state is PluginActivationState.missing
    assert "history" in repository.rows


def _cli_patch(manager, tmp_path):
    settings = SimpleNamespace(
        resolved_plugins_dir=tmp_path / "legacy",
        data_dir=tmp_path,
        db_url="postgresql+asyncpg://example",
    )
    container = SimpleNamespace(close=AsyncMock())
    return patch(
        "research_engine.cli.plugin._open",
        new=AsyncMock(return_value=(settings, container, manager)),
    )


def test_enable_reviews_and_records_noninteractive_approval(tmp_path) -> None:
    plugin = _discovered(tmp_path)
    status = PluginStatus(
        "history",
        PluginActivationState.available,
        discovered=plugin,
    )
    approved = PluginActivation(
        plugin_id="history",
        distribution_name=plugin.distribution_name,
        distribution_version=plugin.distribution_version,
        entry_point_name="history",
        manifest_sha256=plugin.manifest_sha256,
        manifest=plugin.manifest.model_dump(mode="json"),
        permissions_granted=plugin.manifest.permissions.model_dump(mode="json"),
        installed_at=datetime.now(UTC),
        approved_at=datetime.now(UTC),
        enabled=True,
        state=PluginActivationState.enabled,
    )
    manager = AsyncMock()
    manager.audit.return_value = status
    manager.approve.return_value = approved

    with _cli_patch(manager, tmp_path):
        result = runner.invoke(plugin_app, ["enable", "history", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert "research-engine-plugin-history==0.2.0" in result.stdout
    assert "Manifest SHA-256" in result.stdout
    assert "Permissions" in result.stdout
    assert "Contributions" in result.stdout
    assert "Enabled history" in result.stdout
    manager.approve.assert_awaited_once_with(
        "history",
        non_interactive=True,
    )


def test_unknown_plugin_prints_pip_and_pipx_commands(tmp_path) -> None:
    manager = AsyncMock()
    manager.audit.side_effect = PluginNotInstalledError("kindle")

    with _cli_patch(manager, tmp_path):
        result = runner.invoke(plugin_app, ["audit", "kindle"])

    assert result.exit_code == 1
    assert "python -m pip install research-engine-plugin-kindle" in result.stdout
    assert "pipx inject research-engine research-engine-plugin-kindle" in result.stdout


def test_cli_has_lifecycle_commands_not_package_manager_commands() -> None:
    registered = {command.name for command in plugin_app.registered_commands}

    assert registered == {
        "list",
        "audit",
        "enable",
        "approve-upgrade",
        "disable",
        "migrate",
        "doctor",
        "forget",
    }
