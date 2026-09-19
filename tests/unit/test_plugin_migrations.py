from __future__ import annotations

import importlib
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


from research_engine.domain.provenance import PluginActivationState
from research_engine.plugins.activation import (
    PluginActivationManager,
    PluginMigrationError,
)
from research_engine.plugins.discovery import DiscoveredPlugin
from research_engine.plugins.loader import PluginLoader
from research_engine.plugins.registry import PluginRegistry
from research_engine_sdk import PluginManifest


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

    async def record_migration(
        self,
        plugin_id,
        *,
        revision,
        status,
        state,
        last_error=None,
    ):
        self.row = self.row.model_copy(
            update={
                "database_revision": revision,
                "database_status": status,
                "state": state,
                "last_error": last_error,
            }
        )

    async def delete(self, plugin_id):
        self.row = None


def _migration_plugin(tmp_path: Path, monkeypatch) -> DiscoveredPlugin:
    package = tmp_path / "migration_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "db.py").write_text(
        "revision = 0\n"
        "calls = []\n"
        "def status(*, context, database_url):\n"
        "    calls.append(('status', context, database_url))\n"
        "    return {'current_revision': revision, 'status': 'ok'}\n"
        "async def upgrade(*, context, database_url):\n"
        "    global revision\n"
        "    calls.append(('upgrade', context, database_url))\n"
        "    revision = 3\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    manifest = PluginManifest.model_validate(
        {
            "schema_version": 2,
            "plugin_id": "migration-fixture",
            "requires": {"core_api": ">=0.6,<0.7"},
            "permissions": {"filesystem": "plugin_data"},
            "provides": {
                "database": {
                    "current_revision": 3,
                    "upgrade_entry": "migration_fixture.db:upgrade",
                    "status_entry": "migration_fixture.db:status",
                }
            },
        }
    )
    manifest_path = package / "plugin.yaml"
    manifest_path.write_text("schema_version: 2\nplugin_id: migration-fixture\n")
    return DiscoveredPlugin(
        plugin_id="migration-fixture",
        module_name="migration_fixture",
        entry_point_name="migration-fixture",
        distribution_name="marginalia-ai-plugin-migration-fixture",
        distribution_version="1.0.0",
        project_urls={},
        direct_url=None,
        manifest=manifest,
        manifest_sha256="c" * 64,
        manifest_bytes=manifest_path.read_bytes(),
        manifest_path=manifest_path,
        package_root=package,
    )


async def test_pending_migration_blocks_load_then_records_explicit_upgrade(
    tmp_path, monkeypatch
) -> None:
    plugin = _migration_plugin(tmp_path, monkeypatch)
    repository = MemoryRepo()
    manager = PluginActivationManager(repository)
    await manager.approve(
        plugin.plugin_id,
        non_interactive=True,
        discovered_plugins=[plugin],
    )
    loader = PluginLoader(repository, PluginRegistry(), tmp_path / "data")

    assert await loader.load_enabled([plugin]) == []
    [status] = await manager.inventory(discovered_plugins=[plugin])
    assert status.state is PluginActivationState.error
    assert status.reason is not None and "migration required" in status.reason

    result = await manager.migrate(
        plugin.plugin_id,
        database_url="postgresql+asyncpg://migration-test",
        data_root=tmp_path / "data",
        discovered_plugins=[plugin],
    )

    assert result["previous_revision"] == 0
    assert result["current_revision"] == 3
    assert repository.row.database_revision == 3
    assert repository.row.database_status == "ok"
    assert repository.row.state is PluginActivationState.enabled
    assert await loader.load_enabled([plugin]) == [plugin.plugin_id]
    module = importlib.import_module("migration_fixture.db")
    upgrade_call = next(call for call in module.calls if call[0] == "upgrade")
    assert upgrade_call[1].plugin_id == plugin.plugin_id
    assert upgrade_call[1].data_dir == (tmp_path / "data" / plugin.plugin_id).resolve()
    assert upgrade_call[1].database_url.get_secret_value() == upgrade_call[2]
    assert upgrade_call[2] == "postgresql+asyncpg://migration-test"


async def test_migration_refuses_changed_unapproved_artifact(
    tmp_path, monkeypatch
) -> None:
    plugin = _migration_plugin(tmp_path, monkeypatch)
    repository = MemoryRepo()
    manager = PluginActivationManager(repository)
    await manager.approve(
        plugin.plugin_id,
        non_interactive=False,
        discovered_plugins=[plugin],
    )
    changed = replace(plugin, manifest_sha256="d" * 64)

    with pytest.raises(PluginMigrationError, match="exact approval"):
        await manager.migrate(
            plugin.plugin_id,
            database_url="postgresql+asyncpg://migration-test",
            data_root=tmp_path / "data",
            discovered_plugins=[changed],
        )
