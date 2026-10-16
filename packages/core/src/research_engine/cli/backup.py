"""CLI backup and restore commands."""

from __future__ import annotations

import asyncio
import os
import subprocess

# Typer resolves these annotations at runtime to build the CLI options, so
# Path must exist at runtime and cannot move into a TYPE_CHECKING block.
from pathlib import Path  # noqa: TC003

import typer
from rich.console import Console

backup_app = typer.Typer()
console = Console()


def libpq_target(db_url: str) -> tuple[str, dict[str, str]]:
    """The URL to hand `pg_dump`/`pg_restore`, and the environment to run them in.

    The password travels in ``PGPASSWORD``, never in argv, where every user on
    the host can read it from the process table. A URL without one leaves
    libpq to find it in ``~/.pgpass`` as usual.
    """
    from sqlalchemy.engine import URL, make_url

    url = make_url(db_url)
    env = dict(os.environ)
    if url.password is not None:
        env["PGPASSWORD"] = str(url.password)
    # Rebuilt rather than `.set(password=None)`, which leaves the password in.
    target = URL.create(
        "postgresql",
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    )
    return target.render_as_string(hide_password=False), env


@backup_app.command("create")
def create(
    output: Path = typer.Argument(..., help="Output file path for the backup."),
):
    """Create a database backup."""
    asyncio.run(_backup(output))


async def _backup(output: Path):
    from research_engine.config import load_settings

    settings = load_settings()
    # A corpus spans core, bibliography, evidence, argument, authored, and
    # plugin-owned schemas. A core-only archive restores only when the omitted
    # schemas happen to survive in the target database; it is not a backup.
    db_url, env = libpq_target(settings.db_url)
    console.print(f"Backing up to {output}...")

    proc = subprocess.run(
        ["pg_dump", db_url, "-Fc", f"--file={output}"],
        capture_output=True,
        text=True,
        env=env,
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
    db_url, env = libpq_target(settings.db_url)
    console.print(f"Restoring from {input_file}...")

    proc = subprocess.run(
        ["pg_restore", "-d", db_url, "--clean", "--if-exists", str(input_file)],
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode == 0:
        console.print("[green]Restore complete[/green]")
    else:
        # pg_restore exits non-zero when it ignored errors, and the database
        # may then be partly restored. That is a failure to script against,
        # not a warning to scroll past.
        console.print(f"[red]Restore failed: {proc.stderr}[/red]")
        raise typer.Exit(1)
