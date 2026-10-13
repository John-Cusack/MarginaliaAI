"""`research-engine tool call` runs a tool from the shell, as MCP would."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from research_engine.cli.tool import tool_app

pytestmark = pytest.mark.unit

runner = CliRunner()


def test_call_runs_a_tool_and_prints_its_result_as_json() -> None:
    container = SimpleNamespace(close=AsyncMock())
    dispatched = AsyncMock(return_value={"loaded": 3, "edition_key": "KJV"})

    with (
        patch(
            "research_engine.composition.build_container",
            AsyncMock(return_value=container),
        ),
        patch("research_engine.config.load_settings", return_value=object()),
        patch("research_engine.mcp.dispatch.dispatch_tool", dispatched),
    ):
        result = runner.invoke(
            tool_app, ["call", "dilemma_ingest", "--json", '{"source_key": "KJV"}']
        )

    assert result.exit_code == 0, result.output
    assert '"loaded": 3' in result.output
    assert dispatched.await_args.args[1:] == ("dilemma_ingest", {"source_key": "KJV"})
    container.close.assert_awaited_once()


def test_call_refuses_arguments_that_are_not_an_object() -> None:
    bad_json = runner.invoke(tool_app, ["call", "x", "--json", "{nope"])
    not_object = runner.invoke(tool_app, ["call", "x", "--json", "[1, 2]"])

    assert bad_json.exit_code == 2
    assert not_object.exit_code == 2


def test_call_names_an_unknown_tool() -> None:
    container = SimpleNamespace(close=AsyncMock())
    with (
        patch(
            "research_engine.composition.build_container",
            AsyncMock(return_value=container),
        ),
        patch("research_engine.config.load_settings", return_value=object()),
        patch(
            "research_engine.mcp.dispatch.dispatch_tool",
            AsyncMock(side_effect=ValueError("Unknown tool: nope")),
        ),
    ):
        result = runner.invoke(tool_app, ["call", "nope"])

    assert result.exit_code == 1
    assert "Is its plugin enabled?" in result.stderr
    container.close.assert_awaited_once()


def test_wake_gpu_starts_the_host_before_the_container() -> None:
    order: list[str] = []
    container = SimpleNamespace(close=AsyncMock())

    async def wake(_settings):
        order.append("wake")
        return "started"

    async def build(_settings):
        order.append("build")
        return container

    with (
        patch("research_engine.adapters.inference.gpu_host.ensure_gpu_host_ready", wake),
        patch("research_engine.composition.build_container", build),
        patch("research_engine.config.load_settings", return_value=object()),
        patch("research_engine.mcp.dispatch.dispatch_tool", AsyncMock(return_value={})),
    ):
        result = runner.invoke(tool_app, ["call", "dilemma_ingest", "--wake-gpu"])

    assert result.exit_code == 0, result.output
    assert order == ["wake", "build"]


def test_without_the_flag_the_host_is_left_alone() -> None:
    container = SimpleNamespace(close=AsyncMock())
    wake = AsyncMock()
    with (
        patch("research_engine.adapters.inference.gpu_host.ensure_gpu_host_ready", wake),
        patch(
            "research_engine.composition.build_container",
            AsyncMock(return_value=container),
        ),
        patch("research_engine.config.load_settings", return_value=object()),
        patch("research_engine.mcp.dispatch.dispatch_tool", AsyncMock(return_value={})),
    ):
        result = runner.invoke(tool_app, ["call", "dilemma_blocked_queue"])

    assert result.exit_code == 0, result.output
    wake.assert_not_awaited()
