"""Run one core or plugin tool from the shell, as the MCP server would."""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console

tool_app = typer.Typer(no_args_is_help=True)
console = Console(stderr=True)


@tool_app.callback()
def tool() -> None:
    """Run core and plugin tools from the shell.

    A callback keeps `call` a subcommand: a one-command Typer app otherwise
    collapses into that command and reads the word `call` as the tool id.
    """


@tool_app.command("call")
def call(
    tool_id: str = typer.Argument(
        ..., help="Tool id, dotted or underscored (dilemma.ingest or dilemma_ingest)."
    ),
    arguments: str = typer.Option(
        "{}", "--json", help="Tool arguments as one JSON object."
    ),
    wake_gpu: bool = typer.Option(
        False,
        "--wake-gpu",
        help="Start the GPU embed host first, as `ingest` does — for tools that embed.",
    ),
) -> None:
    """Run a tool and print its result as JSON on stdout.

    A pack tool gets exactly the scoped clients its approved permissions grant,
    through the same dispatch the MCP transport uses — so it behaves here as it
    does over the wire, without the server running.
    """
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        console.print(f"[red]--json is not valid JSON: {exc}[/red]")
        raise typer.Exit(code=2) from None
    if not isinstance(parsed, dict):
        console.print("[red]--json must be a JSON object of tool arguments.[/red]")
        raise typer.Exit(code=2)
    result = asyncio.run(_call(tool_id, parsed, wake_gpu=wake_gpu))
    typer.echo(json.dumps(result, default=str, indent=2, ensure_ascii=False))


async def _call(tool_id: str, arguments: dict, *, wake_gpu: bool = False) -> object:
    from research_engine.adapters.inference.gpu_host import (
        GpuHostError,
        ensure_gpu_host_ready,
    )
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.mcp.dispatch import dispatch_tool

    settings = load_settings()
    # Opt-in, because most tools never embed: a pack tool that ingests reaches
    # the embedder through its ingestion client, which does not wake the host
    # itself. Like `ingest`, this only ever starts the unit, never stops it.
    if wake_gpu:
        try:
            await ensure_gpu_host_ready(settings)
        except GpuHostError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    container = await build_container(settings)
    try:
        return await dispatch_tool(container, tool_id, arguments)
    except ValueError as exc:
        if str(exc).startswith("Unknown tool"):
            console.print(f"[red]{exc}. Is its plugin enabled?[/red]")
            raise typer.Exit(code=1) from None
        raise
    finally:
        await container.close()
