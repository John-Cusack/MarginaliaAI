"""Explicit database schema management commands."""

from __future__ import annotations

import typer
from alembic import command

from research_engine.adapters.storage.postgres.migrations.runner import migration_config

database_app = typer.Typer(no_args_is_help=True)


@database_app.command("upgrade")
def upgrade(
    revision: str = typer.Argument("head", help="Alembic revision to apply."),
) -> None:
    """Upgrade the configured database schema."""
    command.upgrade(migration_config(), revision)
    typer.echo(f"Database schema upgraded to {revision}.")


@database_app.command("current")
def current() -> None:
    """Show the configured database's current schema revision."""
    command.current(migration_config(), verbose=True)
