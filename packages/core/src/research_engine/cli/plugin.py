"""CLI plugin management commands."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

plugin_app = typer.Typer()
console = Console()

_RESTART_NOTICE = "[yellow]Restart the MCP server for this change to take effect.[/yellow]"


@plugin_app.command("install")
def install(
    url: str = typer.Argument(..., help="Git URL of the plugin."),
    ref: str = typer.Option("main", "--ref", help="Git ref (tag, branch, or SHA)."),
):
    """Install a plugin from a git URL."""
    asyncio.run(_install(url, ref))


async def _install(url: str, ref: str):
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.domain.errors import PluginError
    from research_engine.plugins.installer import PluginInstaller

    settings = load_settings()
    container = await build_container(settings)
    try:
        installer = PluginInstaller(settings.resolved_plugins_dir, container.installed_plugins)
        with console.status("Installing plugin...") as status:

            def _update_status(msg: str) -> None:
                status.update(msg)

            plugin = await installer.install(url, ref, console_callback=_update_status)
        console.print(f"[green]Installed {plugin.id}@{plugin.version}[/green]")
        console.print(_RESTART_NOTICE)
    except PluginError as e:
        console.print(f"[red]Installation failed:[/red] {e}")
        raise typer.Exit(code=1) from None
    finally:
        await container.close()


@plugin_app.command("uninstall")
def uninstall(name: str = typer.Argument(..., help="Plugin name.")):
    """Uninstall a plugin."""
    asyncio.run(_uninstall(name))


async def _uninstall(name: str):
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.plugins.installer import PluginInstaller

    settings = load_settings()
    container = await build_container(settings)
    try:
        installer = PluginInstaller(settings.resolved_plugins_dir, container.installed_plugins)
        await installer.uninstall(name)
        console.print(f"[green]Uninstalled {name}[/green]")
        console.print(_RESTART_NOTICE)
    finally:
        await container.close()


@plugin_app.command("list")
def list_plugins():
    """List installed plugins."""
    asyncio.run(_list())


async def _list():
    from research_engine.composition import build_container
    from research_engine.config import load_settings

    settings = load_settings()
    container = await build_container(settings)
    try:
        plugins = await container.installed_plugins.list_all()
        if not plugins:
            console.print("No plugins installed.")
            return

        table = Table(title="Installed Plugins")
        table.add_column("Name")
        table.add_column("Version")
        table.add_column("Enabled")
        table.add_column("Source")

        for p in plugins:
            table.add_row(p.id, p.version, "Yes" if p.enabled else "No", p.source_url)
        console.print(table)
    finally:
        await container.close()


@plugin_app.command("enable")
def enable(name: str = typer.Argument(...)):
    """Enable a plugin."""
    asyncio.run(_toggle(name, True))


@plugin_app.command("disable")
def disable(name: str = typer.Argument(...)):
    """Disable a plugin."""
    asyncio.run(_toggle(name, False))


async def _toggle(name: str, enabled: bool):
    from research_engine.composition import build_container
    from research_engine.config import load_settings

    settings = load_settings()
    container = await build_container(settings)
    try:
        await container.installed_plugins.set_enabled(name, enabled)
        state = "enabled" if enabled else "disabled"
        console.print(f"[green]Plugin {name} {state}[/green]")
        console.print(_RESTART_NOTICE)
    finally:
        await container.close()
