"""`research-engine dilemma …`: the Dilemma Finder's commands.

Loading profiles and their editions is the `dilemma` pack's work — the pack
holds the editions catalog, the profiles and the source readers — so these
commands run its tools through the same dispatch the MCP server uses, with
exactly the clients its approved permissions grant. Later stages (claims,
the funnel, review) are core services and will sit beside them here.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer

from research_engine.cli.tool import _call

dilemma_app = typer.Typer(no_args_is_help=True)
profile_app = typer.Typer(no_args_is_help=True)
dilemma_app.add_typer(profile_app, name="profile", help="Load and inspect reader profiles.")


def _run(tool_id: str, arguments: dict[str, Any], *, wake_gpu: bool = False) -> None:
    given = {k: v for k, v in arguments.items() if v is not None}
    result = asyncio.run(_call(tool_id, given, wake_gpu=wake_gpu))
    typer.echo(json.dumps(result, default=str, indent=2, ensure_ascii=False))


@profile_app.command("load")
def profile_load(key: str = typer.Argument(..., help="Profile key, e.g. lds.")) -> None:
    """Register the profile's editions and store its roles under its data hash."""
    _run("dilemma.profile_load", {"key": key})


@profile_app.command("show")
def profile_show(
    key: str = typer.Argument(..., help="Profile key."),
    on: str | None = typer.Option(None, "--on", help="Only roles in force on this ISO date."),
) -> None:
    """Show the stored profile, and whether it matches the pack's data."""
    _run("dilemma.profile_show", {"key": key, "on": on})


@dilemma_app.command("ingest")
def ingest(
    profile: str = typer.Argument(..., help="Profile whose editions to load."),
    slice_: str | None = typer.Option(None, "--slice", help="One slice of its manifest."),
    source: str | None = typer.Option(None, "--source", help="One edition it names."),
    stage_only: bool = typer.Option(
        False, "--stage-only", help="Acquire, parse and check; write nothing."
    ),
    accept_mismatch: bool = typer.Option(
        False, "--accept-mismatch",
        help="Load even though coverage does not match; recorded as accepted.",
    ),
    wake_gpu: bool = typer.Option(
        True, "--wake-gpu/--no-wake-gpu", help="Start the GPU embed host first."
    ),
) -> None:
    """Load a slice of a profile's editions (or one edition) as one recorded run."""
    if bool(slice_) == bool(source):
        raise typer.BadParameter("give exactly one of --slice or --source")
    _run(
        "dilemma.ingest",
        {"profile": profile, "slice": slice_, "source_key": source,
         "stage_only": stage_only, "accept_mismatch": accept_mismatch},
        wake_gpu=wake_gpu and not stage_only,
    )


@dilemma_app.command("coverage")
def coverage(source: str = typer.Argument(..., help="Catalog edition key.")) -> None:
    """One edition's acquisition, artifacts and coverage history."""
    _run("dilemma.coverage", {"source_key": source})


@dilemma_app.command("blocked")
def blocked(
    profile: str | None = typer.Option(None, "--profile", help="Only this profile's editions."),
) -> None:
    """The scan and purchase queue, highest tier first."""
    _run("dilemma.blocked_queue", {"profile": profile})
