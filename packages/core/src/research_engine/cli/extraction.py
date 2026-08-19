"""`research-engine extraction` — register the schemas that `extract` runs.

``extract`` resolves a schema by ``name:version`` from the database. Until this
existed there was no way to put one there, so the extraction layer was complete
and unreachable: a table with no door.
"""

from __future__ import annotations

import asyncio

# Path must exist at runtime and cannot move into a TYPE_CHECKING block.
from pathlib import Path  # noqa: TC003

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
