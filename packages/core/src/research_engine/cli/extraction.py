"""`research-engine extraction` — register the schemas that `extract` runs.

``extract`` resolves a schema by ``name:version`` from the database. Until this
existed there was no way to put one there, so the extraction layer was complete
and unreachable: a table with no door.
"""

from __future__ import annotations

import asyncio

# Path must exist at runtime and cannot move into a TYPE_CHECKING block.
from pathlib import Path  # noqa: TC003
from uuid import UUID

import typer

extraction_app = typer.Typer(no_args_is_help=True)


@extraction_app.command("register")
def register(
    file: Path = typer.Argument(..., help="Schema YAML file."),
    owner: str = typer.Option(
        None, "--owner", help="Override the owner declared in the file."
    ),
) -> None:
    """Validate a schema file and store it, replacing that version if present.

    A schema is checked before it is stored — every record type must quote the
    passage it came from, field types must be expressible to the model, and the
    prompt must actually interpolate the passage. Finding those here costs one
    command; finding them mid-run costs a corpus pass.
    """
    from research_engine.domain.errors import ValidationError

    if not file.exists():
        typer.echo(f"No such file: {file}")
        raise typer.Exit(code=1)
    try:
        schema = asyncio.run(_register(file.read_text(), owner))
    except ValidationError as exc:
        typer.echo(f"{file} was not registered.\n\n  {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo(f"Registered {schema.name}:{schema.version} (owner {schema.owner})")
    types = schema.schema_def.get("record_types", [])
    for record_type in types:
        fields = record_type.get("fields", {})
        typer.echo(f"  {record_type.get('id')}  {len(fields)} field(s)")
    typer.echo(f"\nRun it with:  extract schema=\"{schema.name}:{schema.version}\"")


@extraction_app.command("sync")
def sync() -> None:
    """Register every extraction schema the installed packs declare.

    The plugin loader parses these into an in-memory registry, which the
    executor does not read. This copies them where a run can find them.
    """
    registered = asyncio.run(_sync())
    if not registered:
        typer.echo("No installed pack declares an extraction schema.")
        return
    typer.echo(f"Registered {len(registered)} pack schema(s):")
    for schema in registered:
        typer.echo(f"  {schema.name}:{schema.version}  (owner {schema.owner})")


@extraction_app.command("list")
def list_schemas() -> None:
    """List registered schemas and how many extractions each has produced."""
    rows = asyncio.run(_list())
    if not rows:
        typer.echo(
            "No schemas registered.\n\n"
            "  research-engine extraction register <file.yaml>\n"
            "  research-engine extraction sync"
        )
        return
    typer.echo(f"{'SCHEMA':<34} {'OWNER':<16} {'RECORDS':>9} {'PASSAGES':>9}")
    for schema, records, passages in rows:
        name = f"{schema.name}:{schema.version}"
        typer.echo(f"{name:<34} {schema.owner:<16} {records:>9} {passages:>9}")


async def _register(content: str, owner: str | None):
    from research_engine.services.extraction.registration import ExtractionSchemaService

    container, close = await _container()
    try:
        service = ExtractionSchemaService(
            container.extraction_schema_repo, container.transaction_factory
        )
        return await service.register_yaml(content, owner)
    finally:
        await close()


async def _sync():
    from research_engine.services.extraction.registration import ExtractionSchemaService

    container, close = await _container()
    try:
        service = ExtractionSchemaService(
            container.extraction_schema_repo, container.transaction_factory
        )
        return await service.sync_packs(container.plugin_registry)
    finally:
        await close()


async def _list():
    import sqlalchemy as sa

    container, close = await _container()
    try:
        schemas = await container.extraction_schema_repo.list_all()
        counts: dict[object, tuple[int, int]] = {}
        async with container.engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT schema_id, count(*) AS records, "
                        "count(DISTINCT passage_id) AS passages "
                        "FROM core.extraction_records GROUP BY schema_id"
                    )
                )
            ).all()
            counts = {row.schema_id: (row.records, row.passages) for row in rows}
        return [
            (schema, *counts.get(schema.id, (0, 0)))
            for schema in sorted(schemas, key=lambda s: (s.name, s.version))
        ]
    finally:
        await close()


async def _container():
    from research_engine.composition import build_container
    from research_engine.config import load_settings

    container = await build_container(load_settings())
    return container, container.close


@extraction_app.command("run")
def run(
    schema: str = typer.Argument(..., help="Schema as 'name:version', e.g. 'claims:1'."),
    document_id: list[str] = typer.Option(
        None, "--document-id", help="Restrict to these documents. Repeatable."
    ),
    dated_only: bool = typer.Option(
        False,
        "--dated-only",
        help="Only passages sitting inside a structure node that carries a date.",
    ),
    limit: int = typer.Option(0, "--limit", help="Stop after this many passages."),
    concurrency: int = typer.Option(8, "--concurrency", help="Concurrent calls."),
    force_refresh: bool = typer.Option(
        False, "--force-refresh", help="Re-extract passages already done."
    ),
    estimate_only: bool = typer.Option(
        False, "--estimate", help="Report what would run and what it would cost."
    ),
    model: str = typer.Option(
        None,
        "--model",
        help="LLM model for this run, e.g. claude-sonnet-5. Defaults to the configured one.",
    ),
    exclude_range: list[str] = typer.Option(
        None,
        "--exclude-range",
        help=(
            "Skip passages overlapping a character range of a document's canonical "
            "text: DOCUMENT_ID:START-END, or START-END with exactly one --document-id. "
            "Repeatable. For a volume's catalogue, index or front matter."
        ),
    ),
) -> None:
    """Run a registered extraction schema over passages.

    Extraction was previously reachable only through the MCP `extract` tool, so
    running a schema over a corpus meant driving an MCP client. Results are
    stored and cached: re-running returns the same records without paying for
    them again, and `--force-refresh` is how you overrule that.

    `--dated-only` matters for correspondence. A relative date — "yours of the
    3d ult." — resolves only against the date of the letter it appears in, and
    that date lives on the passage's structure node. Passages outside a dated
    node extract fine and their relative dates stay unresolved.
    """
    from research_engine.domain.errors import LLMUnavailable

    ids = [UUID(d) for d in document_id] if document_id else None
    try:
        excluded = parse_exclude_ranges(exclude_range or [], ids)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from exc
    try:
        result = asyncio.run(
            _run_extraction(
                schema,
                ids,
                dated_only,
                limit,
                concurrency,
                force_refresh,
                estimate_only,
                model=model,
                excluded=excluded,
            )
        )
    except LLMUnavailable as exc:
        typer.echo(f"\nStopped before extracting anything: {exc}")
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.echo(f"{exc}\n\nRegistered schemas: research-engine extraction list")
        raise typer.Exit(code=1) from exc

    _print_run(result)
    if result.get("failed"):
        raise typer.Exit(code=1)


def parse_exclude_ranges(
    values: list[str], document_ids: list[UUID] | None
) -> dict[UUID, list[tuple[int, int]]]:
    """``DOCUMENT_ID:START-END`` (or ``START-END`` with one document) by document."""
    ranges: dict[UUID, list[tuple[int, int]]] = {}
    for value in values:
        document, _, span = value.rpartition(":")
        if document:
            target = UUID(document)
        elif document_ids and len(document_ids) == 1:
            target = document_ids[0]
        else:
            raise ValueError(
                f"--exclude-range {value!r} names no document: write "
                f"DOCUMENT_ID:START-END, or give exactly one --document-id."
            )
        start_text, _, end_text = span.partition("-")
        try:
            start, end = int(start_text), int(end_text)
        except ValueError as exc:
            raise ValueError(f"--exclude-range {value!r}: expected START-END") from exc
        if start >= end:
            raise ValueError(f"--exclude-range {value!r}: START must be below END")
        ranges.setdefault(target, []).append((start, end))
    return ranges


def overlaps_excluded(
    document_id: UUID,
    char_start: int | None,
    char_end: int | None,
    excluded: dict[UUID, list[tuple[int, int]]],
) -> bool:
    if char_start is None or char_end is None:
        return False
    return any(
        char_start < end and start < char_end
        for start, end in excluded.get(document_id, [])
    )


async def _run_extraction(
    schema,
    document_ids,
    dated_only,
    limit,
    concurrency,
    force_refresh,
    estimate_only,
    *,
    model=None,
    excluded=None,
):
    import sqlalchemy as sa

    from research_engine.domain.extractions import ExtractionOptions

    container, close = await _container()
    try:
        passages = await _select_passages(
            container.engine, document_ids, dated_only, limit, excluded=excluded
        )
        if estimate_only or not passages:
            return {
                "estimate_only": True,
                "selected": len(passages),
                "dated": sum(1 for p in passages if p[2]),
                "input_tokens": sum(p[1] for p in passages),
            }

        batch = await container.extraction_executor.execute(
            [p[0] for p in passages],
            schema,
            ExtractionOptions(
                concurrency=concurrency, force_refresh=force_refresh, llm_model=model
            ),
        )
        results = batch.results
        records = [record for r in results for record in r.records]
        async with container.engine.connect() as conn:
            spend = (
                await conn.execute(
                    sa.text(
                        "SELECT count(*), coalesce(sum(cost_estimate), 0) "
                        "FROM core.llm_calls WHERE purpose LIKE 'extraction%'"
                    )
                )
            ).first()
        return {
            "estimate_only": False,
            "selected": len(passages),
            "ok": sum(1 for r in results if r.status == "ok"),
            "failed": [r for r in results if r.status == "failed"],
            "cached": sum(1 for r in results if r.from_cache),
            "records": len(records),
            "resolved_dates": sum(
                1
                for record in records
                for key, value in record.get("fields", {}).items()
                if key.endswith("_resolved") and value and "start" in value
            ),
            "llm_calls": spend[0] if spend else 0,
            "spend": float(spend[1]) if spend else 0.0,
        }
    finally:
        await close()


async def _select_passages(engine, document_ids, dated_only, limit, excluded=None):
    """Passage ids with their size, and whether their node carries a date.

    *excluded* drops passages overlapping a character range of their document
    — a volume's publisher's catalogue, its index — before *limit* applies, so
    the limit counts passages that will actually run.
    """
    import sqlalchemy as sa

    from research_engine.services.text.tokens import approx_tokens, chars_per_token

    stmt = (
        "SELECT p.id, p.text, n.metadata->>'date_start' AS dated, "
        "p.document_id, p.char_start, p.char_end "
        "FROM core.passages p LEFT JOIN core.document_nodes n ON n.id = p.node_id"
    )
    clauses, params = [], {}
    if document_ids:
        clauses.append("p.document_id = ANY(:ids)")
        params["ids"] = list(document_ids)
    if dated_only:
        clauses.append("n.metadata->>'date_start' IS NOT NULL")
    if clauses:
        stmt += " WHERE " + " AND ".join(clauses)
    stmt += " ORDER BY p.document_id, p.position"
    if limit and not excluded:
        stmt += f" LIMIT {int(limit)}"
    async with engine.connect() as conn:
        rows = (await conn.execute(sa.text(stmt), params)).all()
    if excluded:
        rows = [row for row in rows if not overlaps_excluded(row[3], row[4], row[5], excluded)]
        if limit:
            rows = rows[: int(limit)]
    return [
        (row[0], approx_tokens(row[1], chars_per_token(row[1])), bool(row[2]))
        for row in rows
    ]


def _print_run(result: dict) -> None:
    if result["estimate_only"]:
        selected = result["selected"]
        if not selected:
            typer.echo("No passages matched. Nothing to extract.")
            return
        # Prompt and schema ride along with every call; 500 tokens is the
        # measured overhead for a schema of this size.
        tokens_in = result["input_tokens"] + 500 * selected
        typer.echo(f"Passages selected:    {selected}")
        typer.echo(f"  inside a dated node: {result['dated']}")
        typer.echo(f"Estimated input:      ~{tokens_in / 1000:,.0f}k tokens")
        typer.echo(
            f"Estimated cost:       ~${tokens_in / 1e6 * 3.0 + selected * 350 / 1e6 * 15.0:,.2f}"
            f"  (Sonnet list pricing, before caching)"
        )
        typer.echo("\nDrop --estimate to run it.")
        return

    typer.echo(f"Passages:             {result['selected']}")
    typer.echo(f"  extracted:          {result['ok']}")
    typer.echo(f"  served from cache:  {result['cached']}")
    typer.echo(f"Records stored:       {result['records']}")
    typer.echo(f"  with a resolved date/entity: {result['resolved_dates']}")
    typer.echo(f"LLM calls to date:    {result['llm_calls']}  (${result['spend']:,.2f})")

    failed = result["failed"]
    if failed:
        typer.echo(f"\n{len(failed)} passage(s) failed:")
        for r in failed[:10]:
            typer.echo(f"  {r.passage_id}: {str(r.error)[:120]}")
        if len(failed) > 10:
            typer.echo(f"  ... and {len(failed) - 10} more")
    else:
        typer.echo("\nDone. Query them with `query_extractions`.")
