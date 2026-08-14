"""`research-engine usage` — what the corpus has spent on LLM calls."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import typer

usage_app = typer.Typer(no_args_is_help=False)


@usage_app.callback(invoke_without_command=True)
def usage(
    ctx: typer.Context,
    days: int = typer.Option(30, "--days", "-d", help="Window size, in days."),
    group_by: str = typer.Option(
        "purpose,caller,model",
        "--group-by",
        "-g",
        help="Comma-separated columns: purpose, caller, model, status.",
    ),
) -> None:
    """Report LLM spend and token usage over a window."""
    if ctx.invoked_subcommand is not None:
        return
    asyncio.run(_usage(days, [g.strip() for g in group_by.split(",") if g.strip()]))


async def _usage(days: int, group_by: list[str]) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings

    settings = load_settings()
    container = await build_container(settings)
    try:
        since = container.clock.now() - timedelta(days=days)
        summary = await container.llm_calls.usage_summary(
            since=since, group_by=tuple(group_by)
        )

        if not summary.groups:
            typer.echo(f"No LLM calls in the last {days} days.")
            return

        headers = [*group_by, "calls", "in", "out", "cost", "err"]
        rows = [
            [
                *[str(g.key[col]) for col in group_by],
                str(g.calls),
                f"{g.input_tokens:,}",
                f"{g.output_tokens:,}",
                f"${g.cost:.4f}",
                str(g.errors) if g.errors else "",
            ]
            for g in summary.groups
        ]
        widths = [
            max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))
        ]

        def line(cells: list[str]) -> str:
            return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

        typer.echo(line(headers))
        typer.echo("  ".join("-" * w for w in widths))
        for row in rows:
            typer.echo(line(row))
        typer.echo("")
        typer.echo(
            f"{summary.total_calls} calls, ${summary.total_cost:.4f} estimated "
            f"since {since.date()}"
        )
        if settings.llm_budget_usd is not None:
            remaining = settings.llm_budget_usd - summary.total_cost
            state = (
                f"${remaining:.2f} remaining"
                if remaining >= 0
                else f"over by ${-remaining:.2f} — calls are being refused"
            )
            typer.echo(
                f"Budget: ${settings.llm_budget_usd:.2f} per "
                f"{settings.llm_budget_window_days}d — {state}"
            )
        typer.echo("Costs are estimates recorded at call time, not billed amounts.")
    finally:
        await container.close()
