"""`research-engine config` — show how configuration actually resolved."""

from __future__ import annotations

import typer

config_app = typer.Typer(no_args_is_help=True)

_SOURCE_ORDER = {"override": 0, "environment": 1, "env file": 2, "default": 3}


@config_app.command("show")
def show(
    all_settings: bool = typer.Option(
        False, "--all", "-a", help="Include settings still on their default value."
    ),
) -> None:
    """Print every setting, its value, and where that value came from.

    Configuration that silently fails to load is worse than configuration that is
    absent: an unset spend ceiling reads as protection. This says which file was
    read, whether it existed, and which values actually came from it.
    """
    from research_engine.config.settings import (
        Settings,
        describe_settings,
        find_env_file,
    )

    resolution = find_env_file()
    settings = Settings(_env_file=resolution.path)  # type: ignore[arg-type]
    reports = describe_settings(settings, resolution)

    if resolution.path is None:
        typer.echo(f"env file: (none) — {resolution.reason}")
        typer.echo(
            "         Settings come from environment variables and defaults only.\n"
            "         Set RE_ENV_FILE=/path/to/.env to point at one explicitly."
        )
    else:
        typer.echo(f"env file: {resolution.path}  ({resolution.reason})")
    typer.echo("")

    shown = [r for r in reports if all_settings or r.source != "default"]
    if not shown:
        typer.echo("Every setting is on its default value. Use --all to list them.")
        return

    shown.sort(key=lambda r: (_SOURCE_ORDER.get(r.source, 9), r.name))

    name_w = max(len(r.name) for r in shown)
    value_w = min(max(len(r.value) for r in shown), 60)

    typer.echo(f"{'SETTING'.ljust(name_w)}  {'VALUE'.ljust(value_w)}  SOURCE")
    typer.echo(f"{'-' * name_w}  {'-' * value_w}  {'-' * 11}")
    for r in shown:
        value = r.value if len(r.value) <= value_w else r.value[: value_w - 1] + "…"
        typer.echo(f"{r.name.ljust(name_w)}  {value.ljust(value_w)}  {r.source}")

    if not all_settings:
        hidden = len(reports) - len(shown)
        if hidden:
            typer.echo(f"\n{hidden} setting(s) on defaults — use --all to show them.")
