"""CLI serve command for MCP server."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console

serve_app = typer.Typer()
console = Console()


@serve_app.callback(invoke_without_command=True)
def serve(
    mcp: bool = typer.Option(True, "--mcp/--no-mcp", help="Start MCP stdio server."),
):
    """Start the MCP server."""
    if mcp:
        console.print("[bold]Starting MCP server (stdio)...[/bold]")
        asyncio.run(_serve_mcp())
    else:
        console.print("Only MCP mode is supported in v1.")


async def _serve_mcp():
    from research_engine.config import load_settings
    from research_engine.runtime import configure_logging, run_mcp_server

    settings = load_settings()
    configure_logging(settings)
    await run_mcp_server(settings)
