"""CLI ingest commands."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.progress import Progress

if TYPE_CHECKING:
    from pathlib import Path

ingest_app = typer.Typer()
console = Console()


@ingest_app.callback(invoke_without_command=True)
def ingest(
    sources: list[Path] = typer.Argument(..., help="Files or directories to ingest."),
    plugin: str | None = typer.Option(None, "--plugin", "-p", help="Hint a specific ingestion module."),
    concurrency: int = typer.Option(4, "--concurrency", "-c", help="Parallel ingestion workers."),
):
    """Ingest documents from files or directories."""
    asyncio.run(_ingest(sources, plugin, concurrency))


async def _ingest(sources: list[Path], plugin: str | None, concurrency: int):
    from research_engine.composition import build_container
    from research_engine.config import load_settings

    settings = load_settings(ingest_concurrency=concurrency)
    container = await build_container(settings)
    try:
        with Progress(console=console) as progress:
            task = progress.add_task("Ingesting...", total=None)
            stats = await container.ingestion.ingest_paths(sources, plugin_hint=plugin)
            progress.update(task, completed=True)

        console.print("\n[bold green]Ingestion complete:[/bold green]")
        console.print(f"  Total:   {stats['total']}")
        console.print(f"  OK:      {stats['ok']}")
        console.print(f"  Skipped: {stats['skipped']}")
        console.print(f"  Failed:  {stats['failed']}")
    finally:
        await container.close()
