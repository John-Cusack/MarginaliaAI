"""Manage installed plugin distributions and explicit approvals."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from research_engine.domain.provenance import PluginActivationState
from research_engine.plugins.activation import (
    PluginActivationManager,
    PluginNotInstalledError,
    PluginStatus,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.config import Settings

plugin_app = typer.Typer(no_args_is_help=True)
console = Console()
_RESTART_NOTICE = "[yellow]Restart the MCP server for this change to take effect.[/yellow]"


def _installation_help(plugin_id: str) -> str:
    distribution = f"research-engine-plugin-{plugin_id}"
    return (
        f"Plugin '{plugin_id}' is not installed in this Python environment.\n"
        "Install it with:\n"
        f"  python -m pip install {distribution}\n"
        "For pipx:\n"
        f"  pipx inject research-engine {distribution}"
    )


def _confirm(action: str, *, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        console.print(f"[red]{action} refused in non-interactive mode; pass --yes.[/red]")
        raise typer.Exit(code=2)
    if not typer.confirm(f"{action}?"):
        raise typer.Abort()


def _print_review(status: PluginStatus) -> None:
    plugin = status.discovered
    if plugin is None:
        console.print(_installation_help(status.plugin_id))
        return
    manifest = plugin.manifest
    console.print(f"[bold]{plugin.plugin_id}[/bold]")
    console.print(
        f"Distribution: {plugin.distribution_name}=={plugin.distribution_version}"
    )
    console.print(f"Entry point: {plugin.entry_point_name} = {plugin.module_name}")
    console.print(f"Manifest SHA-256: {plugin.manifest_sha256}")
    if plugin.project_urls:
        console.print("Project URLs:")
        for label, url in sorted(plugin.project_urls.items()):
            console.print(f"  {label}: {url}")
    console.print("Permissions:")
    console.print_json(json.dumps(manifest.permissions.model_dump(mode="json")))
    console.print("Contributions:")
    console.print_json(json.dumps(manifest.provides.model_dump(mode="json")))
    if manifest.provides.database is None:
        console.print("Database migration: none")
    else:
        console.print(
            "Database migration: "
            f"revision {manifest.provides.database.current_revision}; "
            f"upgrade {manifest.provides.database.upgrade_entry}; "
            f"status {manifest.provides.database.status_entry}"
        )


@dataclass(frozen=True)
class _Resources:
    """The database handle the plugin commands need, and nothing else."""

    engine: AsyncEngine

    async def close(self) -> None:
        await self.engine.dispose()


async def _open() -> tuple[Settings, _Resources, PluginActivationManager]:
    """Open the activation store.

    Deliberately not ``build_container``: discovering, auditing and approving a
    plugin needs the database and the installed distributions — not embeddings,
    reranking or an LLM. Building the container made every one of these commands
    fail on a base install, which has no local model ("Local embedding support is
    not installed"), and `plugin list` is how an operator finds that out.
    """
    from research_engine.adapters.storage.postgres.engine import build_engine
    from research_engine.adapters.storage.postgres.repositories.plugins import (
        PGPluginActivationRepo,
    )
    from research_engine.config import load_settings

    settings = load_settings()
    engine = await build_engine(settings.db_url)
    manager = PluginActivationManager(PGPluginActivationRepo(engine))
    return settings, _Resources(engine), manager


@plugin_app.command("list")
def list_plugins() -> None:
    """List available distributions and persisted activation state."""

    asyncio.run(_list())


async def _list() -> None:
    _settings, container, manager = await _open()
    try:
        statuses = await manager.inventory(persist=True)
        if not statuses:
            console.print("No plugin distributions or activation records found.")
            return
        table = Table(title="Plugins")
        table.add_column("Plugin")
        table.add_column("State")
        table.add_column("Distribution")
        table.add_column("Version")
        table.add_column("Detail")
        for status in statuses:
            plugin = status.discovered
            activation = status.activation
            table.add_row(
                status.plugin_id,
                status.state.value,
                plugin.distribution_name
                if plugin is not None
                else (activation.distribution_name if activation else ""),
                plugin.distribution_version
                if plugin is not None
                else (activation.distribution_version if activation else ""),
                status.reason or "",
            )
        console.print(table)
    finally:
        await container.close()


@plugin_app.command("audit")
def audit(plugin_id: str = typer.Argument(..., help="Plugin id.")) -> None:
    """Show static distribution metadata, contributions, and approval state."""

    asyncio.run(_audit(plugin_id))


async def _audit(plugin_id: str) -> None:
    _settings, container, manager = await _open()
    try:
        try:
            status = await manager.audit(plugin_id)
        except PluginNotInstalledError:
            console.print(_installation_help(plugin_id))
            raise typer.Exit(code=1) from None
        console.print(f"State: {status.state.value}")
        if status.reason:
            console.print(f"Reason: {status.reason}")
        _print_review(status)
        if status.activation is not None:
            console.print("Approved snapshot:")
            console.print_json(
                json.dumps(status.activation.model_dump(mode="json"))
            )
    finally:
        await container.close()


@plugin_app.command("enable")
def enable(
    plugin_id: str = typer.Argument(..., help="Plugin id."),
    yes: bool = typer.Option(False, "--yes", help="Approve without an interactive prompt."),
) -> None:
    """Approve the exact installed artifact and enable it."""

    asyncio.run(_enable(plugin_id, yes=yes, upgrade=False))


@plugin_app.command("approve-upgrade")
def approve_upgrade(
    plugin_id: str = typer.Argument(..., help="Plugin id."),
    yes: bool = typer.Option(False, "--yes", help="Approve without an interactive prompt."),
) -> None:
    """Approve an installed version or manifest that differs from the prior approval."""

    asyncio.run(_enable(plugin_id, yes=yes, upgrade=True))


async def _enable(plugin_id: str, *, yes: bool, upgrade: bool) -> None:
    _settings, container, manager = await _open()
    try:
        try:
            status = await manager.audit(plugin_id)
        except PluginNotInstalledError:
            console.print(_installation_help(plugin_id))
            raise typer.Exit(code=1) from None
        if status.discovered is None:
            console.print(_installation_help(plugin_id))
            raise typer.Exit(code=1)
        if upgrade and status.state is not PluginActivationState.pending_approval:
            console.print(f"Plugin '{plugin_id}' has no pending upgrade to approve.")
            raise typer.Exit(code=1)
        if not upgrade and status.state is PluginActivationState.pending_approval:
            console.print(
                f"Plugin '{plugin_id}' changed since approval; use approve-upgrade."
            )
            raise typer.Exit(code=1)
        _print_review(status)
        action = (
            f"Approve upgrade and enable plugin '{plugin_id}'"
            if upgrade
            else f"Approve and enable plugin '{plugin_id}'"
        )
        _confirm(action, yes=yes)
        activation = await manager.approve(
            plugin_id,
            non_interactive=yes,
        )
        console.print(
            f"[green]Enabled {activation.plugin_id} from "
            f"{activation.distribution_name}=={activation.distribution_version}[/green]"
        )
        console.print(_RESTART_NOTICE)
    finally:
        await container.close()


@plugin_app.command("disable")
def disable(plugin_id: str = typer.Argument(..., help="Plugin id.")) -> None:
    """Disable an approved plugin without uninstalling its distribution."""

    asyncio.run(_disable(plugin_id))


async def _disable(plugin_id: str) -> None:
    _settings, container, manager = await _open()
    try:
        try:
            await manager.disable(plugin_id)
        except PluginNotInstalledError:
            console.print(_installation_help(plugin_id))
            raise typer.Exit(code=1) from None
        console.print(f"[green]Disabled {plugin_id}[/green]")
        console.print(_RESTART_NOTICE)
    finally:
        await container.close()


@plugin_app.command("migrate")
def migrate(
    plugin_id: str = typer.Argument(..., help="Plugin id."),
    yes: bool = typer.Option(False, "--yes", help="Run without an interactive prompt."),
) -> None:
    """Run the approved plugin's explicitly declared database upgrade."""

    asyncio.run(_migrate(plugin_id, yes=yes))


async def _migrate(plugin_id: str, *, yes: bool) -> None:
    settings, container, manager = await _open()
    try:
        status = await manager.audit(plugin_id)
        _print_review(status)
        _confirm(f"Run database migration for plugin '{plugin_id}'", yes=yes)
        result = await manager.migrate(
            plugin_id,
            database_url=settings.db_url,
            data_root=settings.data_dir / "plugin-data",
        )
        console.print_json(json.dumps(result, default=str))
    except PluginNotInstalledError:
        console.print(_installation_help(plugin_id))
        raise typer.Exit(code=1) from None
    finally:
        await container.close()


@plugin_app.command("doctor")
def doctor(plugin_id: str | None = typer.Argument(None, help="Optional plugin id.")) -> None:
    """Report invalid, missing, pending, and legacy plugin state."""

    asyncio.run(_doctor(plugin_id))


async def _doctor(plugin_id: str | None) -> None:
    settings, container, manager = await _open()
    try:
        statuses = await manager.inventory(persist=True)
        selected = [
            status
            for status in statuses
            if plugin_id is None or status.plugin_id == plugin_id
        ]
        if plugin_id is not None and not selected:
            console.print(_installation_help(plugin_id))
            raise typer.Exit(code=1)
        for status in selected:
            message = f"{status.plugin_id}: {status.state.value}"
            if status.reason:
                message += f" — {status.reason}"
            console.print(message)
        legacy_dir = settings.resolved_plugins_dir
        if legacy_dir.is_dir():
            for path in sorted(legacy_dir.iterdir(), key=lambda item: item.name):
                console.print(
                    f"Legacy directory preserved (never loaded or deleted): {path}"
                )
    finally:
        await container.close()


@plugin_app.command("forget")
def forget(
    plugin_id: str = typer.Argument(..., help="Plugin id."),
    yes: bool = typer.Option(False, "--yes", help="Remove audit state without prompting."),
) -> None:
    """Remove activation/audit state only; never uninstall the distribution."""

    asyncio.run(_forget(plugin_id, yes=yes))


async def _forget(plugin_id: str, *, yes: bool) -> None:
    _settings, container, manager = await _open()
    try:
        _confirm(f"Forget activation state for plugin '{plugin_id}'", yes=yes)
        try:
            await manager.forget(plugin_id)
        except PluginNotInstalledError:
            console.print(_installation_help(plugin_id))
            raise typer.Exit(code=1) from None
        console.print(
            f"[green]Forgot activation state for {plugin_id}.[/green] "
            "The Python distribution and plugin data were not changed."
        )
    finally:
        await container.close()
