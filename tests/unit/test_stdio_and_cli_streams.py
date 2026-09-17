"""stdout belongs to the MCP transport, and plugin commands run on a base install.

Two regressions this pins, both found by installing base core plus a plugin wheel
in a clean venv:

1. ``research-engine plugin list`` built the whole container, so it died with
   "Local embedding support is not installed" — on the very command an operator
   uses to see whether a plugin is installed.
2. ``serve`` logged to stdout, which is the MCP stdio transport. The client
   reported a JSON-RPC parse error for every log line.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog

from research_engine.cli import plugin as plugin_cli
from research_engine.cli import serve as serve_cli
from research_engine.plugins.activation import PluginActivationManager
from research_engine.runtime import configure_logging


@pytest.fixture(autouse=True)
def _restore_structlog():
    yield
    structlog.reset_defaults()


async def test_plugin_commands_open_only_the_activation_store(monkeypatch) -> None:
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(
        "research_engine.adapters.storage.postgres.engine.build_engine",
        AsyncMock(return_value=engine),
    )
    monkeypatch.setattr(
        "research_engine.composition.build_container",
        AsyncMock(side_effect=AssertionError("plugin commands must not build the container")),
    )

    settings, resources, manager = await plugin_cli._open()

    assert isinstance(manager, PluginActivationManager)
    assert settings.db_url
    await resources.close()
    engine.dispose.assert_awaited_once()


def test_logs_go_to_stderr(capsys) -> None:
    configure_logging(SimpleNamespace(log_format="pretty", log_level="INFO"))

    structlog.get_logger("test").info("settings_loaded")

    captured = capsys.readouterr()
    assert "settings_loaded" in captured.err
    assert captured.out == ""


def test_nothing_logs_to_stdout_before_logging_is_configured() -> None:
    """`settings_loaded` is emitted by load_settings(), before configure_logging()."""
    code = (
        "import structlog, research_engine;"
        "structlog.get_logger('test').info('early_line')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True
    )

    assert "early_line" in proc.stderr
    assert proc.stdout == ""


def test_serve_prints_to_stderr(capsys) -> None:
    serve_cli.serve(mcp=False)

    captured = capsys.readouterr()
    assert "Only MCP mode" in captured.err
    assert captured.out == ""
