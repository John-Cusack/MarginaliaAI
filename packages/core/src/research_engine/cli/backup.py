"""CLI backup and restore commands."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import typer
from rich.console import Console

backup_app = typer.Typer()
console = Console()


@backup_app.command("create")
def create(
    output: Path = typer.Argument(..., help="Output file path for the backup."),
):
    """Create a database backup."""
    asyncio.run(_backup(output))


async def _backup(output: Path):
    from research_engine.config import load_settings

    settings = load_settings()
    # Extract connection details from URL
    db_url = settings.db_url.replace("postgresql+asyncpg://", "postgresql://")
    console.print(f"Backing up to {output}...")

    proc = subprocess.run(
        ["pg_dump", db_url, "--schema=core", "-Fc", f"--file={output}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        console.print(f"[green]Backup saved to {output}[/green]")
    else:
        console.print(f"[red]Backup failed: {proc.stderr}[/red]")
        raise typer.Exit(1)


@backup_app.command("restore")
def restore(
    input_file: Path = typer.Argument(..., help="Backup file to restore."),
):
    """Restore from a database backup."""
    asyncio.run(_restore(input_file))


async def _restore(input_file: Path):
    from research_engine.config import load_settings

    settings = load_settings()
    db_url = settings.db_url.replace("postgresql+asyncpg://", "postgresql://")
    console.print(f"Restoring from {input_file}...")

    proc = subprocess.run(
        ["pg_restore", "-d", db_url, "--clean", "--if-exists", str(input_file)],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        console.print("[green]Restore complete[/green]")
    else:
        console.print(f"[yellow]Restore completed with warnings: {proc.stderr}[/yellow]")
