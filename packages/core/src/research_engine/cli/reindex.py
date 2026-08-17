"""`research-engine reindex` — re-chunk the corpus without losing links."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import UUID

import typer

if TYPE_CHECKING:
    from research_engine.services.ingestion.reindex import ReindexReport

reindex_app = typer.Typer(no_args_is_help=True)


@reindex_app.command("text")
def text(
    document_id: list[str] = typer.Option(
        None, "--document-id", help="Restrict to these documents. Repeatable."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what is recoverable, and by what route."
    ),
    include_slow: bool = typer.Option(
        False, "--include-slow", help="Also re-parse sources that need docling."
    ),
) -> None:
    """Recover canonical text for documents that have none.

    Passage offsets address canonical text; documents ingested before that table
    existed have none, so they cannot be quote-verified or re-anchored.

    This only stores the text. Run `reindex chunks` afterwards to re-anchor the
    passages onto it — its orphan report is what tells you whether the recovered
    text really matches what those passages were cut from.
    """
    from research_engine.services.ingestion.text_backfill import Route

    ids = [UUID(d) for d in document_id] if document_id else None
    routes = {Route.FAST, Route.SLOW} if include_slow else {Route.FAST}
    report = asyncio.run(_reindex_text(ids, routes, dry_run))
    _print_text_report(report, routes)
    if report.failed:
        raise typer.Exit(code=1)


async def _reindex_text(document_ids, routes, dry_run):
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.services.ingestion.text_backfill import TextBackfillService

    container = await build_container(load_settings())
    try:
        service = TextBackfillService(
            container.engine,
            container.document_texts,
            container.ingestion._dispatcher,  # noqa: SLF001 - the orchestrator owns it
        )
        return await service.recover(document_ids, routes=routes, dry_run=dry_run)
    finally:
        await container.close()


def _print_text_report(report, routes) -> None:
    from research_engine.services.ingestion.text_backfill import Route

    grouped = report.by_route()
    if not report.candidates:
        typer.echo("Every document already has canonical text.")
        return

    if report.dry_run:
        typer.echo("DRY RUN — nothing was written.\n")

    typer.echo(f"{len(report.candidates)} document(s) without canonical text:\n")

    labels = {
        Route.FAST: "recoverable now (lightweight parser)",
        Route.SLOW: "recoverable, but needs docling — use --include-slow",
        Route.UNREACHABLE: "not recoverable by core",
        Route.MISSING_FILE: "source file is gone",
    }
    for route, label in labels.items():
        items = grouped.get(route, [])
        if not items:
            continue
        total_mb = sum(c.size_bytes or 0 for c in items) / 1_000_000
        size = f", {total_mb:,.0f} MB" if total_mb else ""
        typer.echo(f"  {route.value:<14} {len(items):>5}  {label}{size}")

    unreachable = grouped.get(Route.UNREACHABLE, [])
    if unreachable:
        by_type: dict[str, int] = {}
        for candidate in unreachable:
            by_type[candidate.document_type] = by_type.get(candidate.document_type, 0) + 1
        typer.echo("\nNot recoverable by core — re-run the owning pack's ingest:")
        for doc_type, count in sorted(by_type.items(), key=lambda kv: -kv[1]):
            typer.echo(f"  {doc_type:<20} {count:>6}")

    if not report.dry_run:
        typer.echo(f"\nRecovered canonical text for {report.recovered} document(s).")
        if report.recovered:
            typer.echo("Next: `research-engine reindex chunks --dry-run` to check "
                       "whether the passages re-anchor onto it.")
    if report.failed:
        typer.echo(f"\n{len(report.failed)} document(s) failed:")
        for doc_id, error in list(report.failed.items())[:10]:
            typer.echo(f"  {doc_id}: {error}")


@reindex_app.command("chunks")
def chunks(
    document_id: list[str] = typer.Option(
        None, "--document-id", help="Re-chunk only these documents. Repeatable."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Do the work and roll back; report what would happen."
    ),
    orphan_threshold: float = typer.Option(
        0.005,
        "--orphan-threshold",
        help="Abort if more than this fraction of passages cannot be re-anchored.",
    ),
) -> None:
    """Re-chunk documents onto current chunker versions, re-anchoring their links.

    Run with --dry-run across the whole corpus first, and take a backup
    (`research-engine backup create`) before the real pass: after old passages
    are deleted, rollback needs that backup.
    """
    from research_engine.domain.errors import EmbeddingUnavailable

    ids = [UUID(d) for d in document_id] if document_id else None
    try:
        report = asyncio.run(_reindex(ids, dry_run, orphan_threshold))
    except EmbeddingUnavailable as exc:
        # One line naming the cause, rather than a traceback under thousands of
        # halving warnings. This run previously looked healthy for hours while
        # embedding against a host that was switched off.
        typer.echo(f"\nStopped: {exc}")
        typer.echo(
            "\nNothing was left half-written — each document commits or rolls "
            "back on its own. Re-run this command once embedding works."
        )
        raise typer.Exit(code=1) from exc
    _print(report, orphan_threshold)
    if report.aborted or report.exceeded(orphan_threshold) or report.documents_failed:
        raise typer.Exit(code=1)


async def _reindex(
    document_ids: list[UUID] | None, dry_run: bool, orphan_threshold: float
) -> ReindexReport:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.services.ingestion.reindex import ReindexService

    container = await build_container(load_settings())
    try:
        service = ReindexService(
            container.engine,
            container.passages,
            container.document_texts,
            container.embedding,
            orphan_threshold=orphan_threshold,
            embedding_batch_size=container.settings.embedding_batch_size,
        )
        return await service.reindex_chunks(document_ids, dry_run=dry_run)
    finally:
        await container.close()


def _print(report: ReindexReport, threshold: float) -> None:
    if report.aborted:
        typer.echo(
            "ABORTED before writing: the preflight pass could not re-anchor enough "
            "passages.\n"
        )
    elif report.dry_run:
        typer.echo("DRY RUN — nothing was written.\n")

    typer.echo(f"Documents examined:   {report.documents_total}")
    typer.echo(f"  re-chunked:         {report.documents_reindexed}")
    typer.echo(f"  already current:    {report.documents_up_to_date}")
    typer.echo(f"Passages {'replaced' if not report.dry_run else 'to replace'}: "
               f"{report.passages_before} -> {report.passages_after}")

    if report.repointed:
        typer.echo("\nReferences re-anchored:")
        for table, count in sorted(report.repointed.items()):
            typer.echo(f"  {table:<20} {count}")

    if report.documents_without_text:
        typer.echo(
            f"\n{len(report.documents_without_text)} document(s) have no canonical text "
            f"stored and were skipped."
        )
        typer.echo(
            "  These predate the document_texts table. Re-ingest them to make them "
            "re-anchorable; reconstructing text from overlapping chunks would "
            "produce confidently wrong offsets."
        )
        for doc_id in report.documents_without_text[:10]:
            typer.echo(f"  {doc_id}")
        if len(report.documents_without_text) > 10:
            typer.echo(f"  ... and {len(report.documents_without_text) - 10} more")

    if report.collisions:
        typer.echo(
            f"\n{report.collisions} extraction(s) dropped: two old passages mapped onto "
            f"one new passage and (passage, schema, version) is unique. Re-run "
            f"extraction on the affected documents to restore them."
        )

    if report.orphans:
        typer.echo(f"\nUnmatched passages: {len(report.orphans)} "
                   f"({report.orphan_rate:.2%} of {report.passages_before})")
        typer.echo(f"Dependent rows at risk: {report.orphaned_dependents}")
        for orphan in report.orphans[:20]:
            deps = ", ".join(f"{k}={v}" for k, v in sorted(orphan.dependents.items()))
            typer.echo(f"  {orphan.passage_id}  {orphan.reason}  [{deps or 'no dependents'}]")
            typer.echo(f"    {orphan.text_preview!r}")
        if len(report.orphans) > 20:
            typer.echo(f"  ... and {len(report.orphans) - 20} more")

    if report.documents_failed:
        typer.echo(f"\nFailed documents: {len(report.documents_failed)}")
        for doc_id, error in list(report.documents_failed.items())[:10]:
            typer.echo(f"  {doc_id}: {error}")

    if report.exceeded(threshold):
        typer.echo(
            f"\nABORTED: orphan rate {report.orphan_rate:.2%} exceeds the "
            f"{threshold:.2%} threshold."
        )
    elif not report.dry_run and report.documents_reindexed:
        typer.echo("\nDone.")
