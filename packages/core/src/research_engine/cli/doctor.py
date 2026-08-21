"""`research-engine doctor` — check the corpus against its own invariants."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from research_engine.services.diagnostics.corpus_check import CorpusReport

doctor_app = typer.Typer(no_args_is_help=False)

_MARK = {True: "FAIL", False: "ok"}


@doctor_app.callback(invoke_without_command=True)
def doctor(
    ctx: typer.Context,
    samples: int = typer.Option(
        5, "--samples", help="Offending ids to show per failing check."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="List passing checks too."
    ),
) -> None:
    """Check stored data against the invariants the test suite asserts on fixtures.

    Tests prove the code is right today; they say nothing about rows written by
    code that was wrong yesterday. This asks the same questions of the corpus.

    Reports only — nothing is written. Each failure names the command that
    repairs it. Exits non-zero when a critical check fails, so it can gate a
    backfill or a release.
    """
    if ctx.invoked_subcommand is not None:
        return
    report = asyncio.run(_run(samples))
    _print(report, verbose=verbose)
    if report.critical:
        raise typer.Exit(code=1)


async def _run(sample_size: int) -> CorpusReport:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.services.diagnostics.corpus_check import CorpusChecker

    container = await build_container(load_settings())
    try:
        return await CorpusChecker(container.engine).run(sample_size=sample_size)
    finally:
        await container.close()


def _print(report: CorpusReport, *, verbose: bool) -> None:
    checks = report.sorted_checks()
    total = checks[0].total if checks else 0
    typer.echo(f"Corpus check — {total or 0} passages\n")

    for check in checks:
        if check.skipped:
            continue
        if not check.failed and not verbose:
            continue
        mark = _MARK[check.failed]
        line = f"  [{mark:>4}] {check.name}"
        if check.failed:
            line += f"  ({check.count})"
        typer.echo(line)
        if not check.failed:
            continue
        typer.echo(f"         {check.description}")
        for sample in check.samples:
            typer.echo(f"         - {sample}")
        if check.remedy:
            typer.echo(f"         fix: {check.remedy}")
        typer.echo("")

    if report.skipped:
        # A check that could not run verified nothing. Saying so is the whole
        # difference between "clean" and "unexamined".
        typer.echo(f"  {len(report.skipped)} check(s) could not run:")
        for check in report.skipped:
            typer.echo(f"         - {check.name}: {check.description.split('— could not run: ')[-1]}")
        typer.echo("")

    failures = report.failures
    if not failures:
        typer.echo("  All checks that ran passed.")
        return

    critical = len(report.critical)
    typer.echo(
        f"\n{len(failures)} check(s) failed, {critical} critical."
        if critical
        else f"\n{len(failures)} check(s) failed, none critical."
    )
