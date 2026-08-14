"""Tests for plugin CLI commands and the restart-required notice."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from research_engine.cli.plugin import plugin_app
from research_engine.domain.errors import PluginError
from research_engine.domain.provenance import InstalledPlugin

runner = CliRunner()

_RESTART_SUBSTRING = "Restart the MCP server"


def _stub_plugin() -> InstalledPlugin:
    return InstalledPlugin(
        id="testpack",
        version="0.1.0",
        source_url="https://example.invalid/repo",
        source_ref="abc123",
        installed_at=datetime.now(UTC),
        enabled=True,
        manifest={},
        permissions_granted={},
    )


@pytest.fixture
def patched_cli():
    """Patch settings, container, and installer used by plugin CLI commands."""
    fake_container = MagicMock()
    fake_container.installed_plugins.set_enabled = AsyncMock()
    fake_container.close = AsyncMock()

    fake_settings = MagicMock()
    fake_settings.resolved_plugins_dir = "/tmp/plugins"

    with (
        patch(
            "research_engine.composition.build_container",
            new=AsyncMock(return_value=fake_container),
        ),
        patch(
            "research_engine.config.load_settings",
            new=MagicMock(return_value=fake_settings),
        ),
        patch("research_engine.plugins.installer.PluginInstaller") as installer_cls,
    ):
        installer_instance = installer_cls.return_value
        installer_instance.install = AsyncMock(return_value=_stub_plugin())
        installer_instance.uninstall = AsyncMock()
        yield {
            "container": fake_container,
            "installer_instance": installer_instance,
        }


def test_install_prints_restart_notice(patched_cli):
    result = runner.invoke(plugin_app, ["install", "https://example.invalid/repo"])
    assert result.exit_code == 0, result.stdout
    assert "Installed testpack@0.1.0" in result.stdout
    assert _RESTART_SUBSTRING in result.stdout


def test_uninstall_prints_restart_notice(patched_cli):
    result = runner.invoke(plugin_app, ["uninstall", "testpack"])
    assert result.exit_code == 0, result.stdout
    assert "Uninstalled testpack" in result.stdout
    assert _RESTART_SUBSTRING in result.stdout


def test_enable_prints_restart_notice(patched_cli):
    result = runner.invoke(plugin_app, ["enable", "testpack"])
    assert result.exit_code == 0, result.stdout
    assert "Plugin testpack enabled" in result.stdout
    assert _RESTART_SUBSTRING in result.stdout


def test_disable_prints_restart_notice(patched_cli):
    result = runner.invoke(plugin_app, ["disable", "testpack"])
    assert result.exit_code == 0, result.stdout
    assert "Plugin testpack disabled" in result.stdout
    assert _RESTART_SUBSTRING in result.stdout


def test_install_failure_omits_restart_notice(patched_cli):
    patched_cli["installer_instance"].install = AsyncMock(
        side_effect=PluginError("boom"),
    )
    result = runner.invoke(plugin_app, ["install", "https://example.invalid/repo"])
    assert result.exit_code == 1
    assert "Installation failed" in result.stdout
    assert _RESTART_SUBSTRING not in result.stdout
