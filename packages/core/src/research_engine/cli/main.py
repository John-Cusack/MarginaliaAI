"""Typer CLI app root."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="research-engine",
    help="Corpus Engine — turn a personal library into a research superpower.",
    no_args_is_help=True,
)


# Import and register subcommands
from research_engine.cli.backup import backup_app
from research_engine.cli.config import config_app
from research_engine.cli.doctor import doctor_app
from research_engine.cli.embed_server import embed_server_app
from research_engine.cli.embeddings import embeddings_app
from research_engine.cli.eval import eval_app
from research_engine.cli.ingest import ingest_app
from research_engine.cli.plugin import plugin_app
from research_engine.cli.reindex import reindex_app
from research_engine.cli.search import search_app
from research_engine.cli.serve import serve_app
from research_engine.cli.usage import usage_app

app.add_typer(ingest_app, name="ingest", help="Ingest documents into the corpus.")
app.add_typer(search_app, name="search", help="Search the corpus.")
app.add_typer(plugin_app, name="plugin", help="Manage plugins.")
app.add_typer(serve_app, name="serve", help="Start the MCP server.")
app.add_typer(backup_app, name="backup", help="Backup and restore.")
app.add_typer(config_app, name="config", help="Inspect configuration.")
app.add_typer(doctor_app, name="doctor", help="Check the corpus against its invariants.")
app.add_typer(embeddings_app, name="embeddings", help="Embedding coverage and repair.")
app.add_typer(embed_server_app, name="embed-server", help="Serve embeddings from this machine's GPU.")
app.add_typer(eval_app, name="eval", help="Measure retrieval quality.")
app.add_typer(usage_app, name="usage", help="Report LLM spend.")
app.add_typer(reindex_app, name="reindex", help="Re-chunk and re-anchor the corpus.")


@app.command()
def status():
    """Show engine status and statistics."""
    import asyncio
    asyncio.run(_status())


async def _status():
    from research_engine.composition import build_container
    from research_engine.config import load_settings

    settings = load_settings()
    container = await build_container(settings)
    try:
        doc_count = await container.docs.count()
        passage_count = await container.passages.count()
        entity_count = await container.entities.count()

        typer.echo(f"Documents:  {doc_count}")
        typer.echo(f"Passages:   {passage_count}")
        typer.echo(f"Entities:   {entity_count}")
        typer.echo(f"LLM model:  {settings.default_llm_model}")
        typer.echo(f"Embedding:  {settings.embedding_model}")
        typer.echo(f"Database:   {settings.db_url}")

        plugins = container.plugin_loader.loaded_plugins
        if plugins:
            typer.echo(f"Plugins:    {', '.join(plugins.keys())}")
        else:
            typer.echo("Plugins:    (none)")
    finally:
        await container.close()


if __name__ == "__main__":
    app()
