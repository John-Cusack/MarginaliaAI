"""`research-engine embeddings` — coverage reporting and repair."""

from __future__ import annotations

import asyncio

import typer

embeddings_app = typer.Typer(no_args_is_help=True)


@embeddings_app.command("status")
def status() -> None:
    """Report embedding coverage for the active model."""
    asyncio.run(_status())


@embeddings_app.command("backfill")
def backfill(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be embedded."),
    limit: int = typer.Option(0, "--limit", help="Cap the number of passages (0 = all)."),
) -> None:
    """Embed passages that have no vector under the active model.

    Also the recovery path for an ingest interrupted partway through embedding.
    Batches commit independently, so this is safe to stop and re-run.
    """
    asyncio.run(_backfill(dry_run, limit or None))


@embeddings_app.command("purge")
def purge(
    model: str = typer.Argument(..., help="Embedding model whose vectors to delete."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the count only."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Delete every vector written by MODEL.

    For clearing vectors a test harness or an abandoned model left behind. The
    active model cannot be purged.
    """
    asyncio.run(_purge(model, dry_run, yes))


async def _service():
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.services.ingestion.embedding_backfill import (
        EmbeddingBackfillService,
    )

    container = await build_container(load_settings())
    service = EmbeddingBackfillService(
        container.engine,
        container.passages,
        container.embedding,
        batch_size=container.settings.embedding_batch_size,
    )
    return container, service


async def _status() -> None:
    container, service = await _service()
    try:
        report = await service.coverage()
        typer.echo(f"Active model: {report.model} ({report.model_version}), dim {report.dim}")
        typer.echo(f"Passages:     {report.total_passages:,}")
        typer.echo(
            f"Embedded:     {report.embedded:,}  ({report.coverage:.1%})"
        )
        if report.missing:
            typer.echo(f"MISSING:      {report.missing:,} — run `embeddings backfill`")
        if report.wrong_dimension:
            typer.echo(
                f"WRONG DIM:    {report.wrong_dimension:,} vectors do not match "
                f"dim {report.dim}"
            )
        if report.foreign_models:
            typer.echo("\nVectors from other models (not used by search):")
            for model, count in sorted(report.foreign_models.items()):
                typer.echo(f"  {model:<30} {count:,}")
            typer.echo("  Remove with `embeddings purge <model>`.")

        if report.missing:
            typer.echo("\nBy document type:")
            for doc_type, total, embedded in await service.coverage_by_document_type():
                flag = "" if embedded == total else "   <-- gap"
                typer.echo(f"  {doc_type:<20} {embedded:>7,}/{total:<7,}{flag}")

        if report.complete and not report.foreign_models:
            typer.echo("\nCoverage is complete.")
    finally:
        await container.close()


async def _backfill(dry_run: bool, limit: int | None) -> None:
    container, service = await _service()
    try:
        report = await service.backfill(dry_run=dry_run, limit=limit)
        if dry_run:
            typer.echo(f"DRY RUN — {report.candidates:,} passage(s) would be embedded.")
            return
        typer.echo(f"Embedded {report.embedded:,} of {report.candidates:,} passage(s).")
        if report.halvings:
            typer.echo(
                f"{report.halvings} batch(es) were split to fit in memory. "
                f"Lower RE_EMBEDDING_BATCH_SIZE to avoid the retries."
            )
        if report.failed_passages:
            typer.echo(
                f"\n{len(report.failed_passages)} passage(s) could not be embedded "
                f"even individually:"
            )
            for pid in report.failed_passages[:10]:
                typer.echo(f"  {pid}")
            if len(report.failed_passages) > 10:
                typer.echo(f"  ... and {len(report.failed_passages) - 10} more")
            raise typer.Exit(code=1)
    finally:
        await container.close()


async def _purge(model: str, dry_run: bool, yes: bool) -> None:
    container, service = await _service()
    try:
        count = await service.purge_model(model, dry_run=True)
        if count == 0:
            typer.echo(f"No vectors from {model!r}.")
            return
        if dry_run:
            typer.echo(f"DRY RUN — would delete {count:,} vector(s) from {model!r}.")
            return
        if not yes and not typer.confirm(f"Delete {count:,} vector(s) from {model!r}?"):
            typer.echo("Aborted.")
            raise typer.Exit(code=1)
        deleted = await service.purge_model(model)
        typer.echo(f"Deleted {deleted:,} vector(s) from {model!r}.")
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    finally:
        await container.close()
