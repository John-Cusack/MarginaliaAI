"""`research-engine eval` — measure retrieval quality across configurations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path  # noqa: TC003 - typer resolves option types at runtime

import typer

eval_app = typer.Typer(no_args_is_help=True)


@eval_app.command("run")
def run(
    queryset: Path = typer.Option(..., "--set", "-s", help="Query set YAML."),
    k: int = typer.Option(10, "--k", help="Cutoff for recall/precision/nDCG."),
    config: list[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="NAME=KEY=VALUE search override, repeatable. "
        "e.g. -c rrf=fusion_mode=rrf -c weighted=fusion_mode=weighted",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Evaluate a query set, optionally comparing configurations.

    Absolute numbers on a hand-built query set mean little. The movement between
    two configurations is the signal, which is why the output is a paired diff
    against the first.
    """
    asyncio.run(_run(queryset, k, config or [], as_json))


async def _run(path: Path, k: int, configs: list[str], as_json: bool) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.eval import QuerySet, compare, run_queryset

    queryset = QuerySet.load(path)
    typer.echo(f"{queryset.name} v{queryset.version}: {len(queryset)} queries\n")

    async def factory():
        return await build_container(load_settings())

    variants: list[tuple[str, dict]] = [("default", {})]
    if configs:
        variants = []
        for spec in configs:
            name, _, override = spec.partition("=")
            key, _, value = override.partition("=")
            variants.append((name, {key: _coerce(value)} if key else {}))

    runs = []
    for name, overrides in variants:
        runs.append(
            await run_queryset(
                factory, queryset, k=k, config_name=name, search_overrides=overrides
            )
        )

    rows = compare(runs)
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return

    headers = list(rows[0])
    widths = [
        max(len(h), *(len(str(row.get(h, ""))) for row in rows)) for h in headers
    ]
    typer.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
    typer.echo("  ".join("-" * w for w in widths))
    for row in rows:
        typer.echo(
            "  ".join(
                str(row.get(h, "")).ljust(w) for h, w in zip(headers, widths, strict=True)
            )
        )

    total_empty = sum(r.empty_queries for r in runs)
    if total_empty:
        typer.echo(
            f"\n{total_empty} query/queries returned nothing. An empty result is "
            f"not a bad ranking — check the filters before reading the averages."
        )


def _coerce(value: str) -> object:
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value
