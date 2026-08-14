"""CLI search commands."""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

search_app = typer.Typer()
console = Console()


@search_app.callback(invoke_without_command=True)
def search(
    query: str = typer.Argument(..., help="Search query."),
    k: int = typer.Option(10, "--k", "-k", help="Number of results."),
    filters: str | None = typer.Option(None, "--filters", "-f", help="JSON filters."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
    no_rerank: bool = typer.Option(False, "--no-rerank", help="Disable reranking."),
):
    """Search the corpus with hybrid retrieval."""
    asyncio.run(_search(query, k, filters, json_output, no_rerank))


async def _search(
    query_text: str, k: int, filters_json: str | None, json_output: bool, no_rerank: bool
):
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.domain.passages import SearchFilters, SearchQuery

    settings = load_settings()
    container = await build_container(settings)
    try:
        filters = None
        if filters_json:
            filters = SearchFilters(**json.loads(filters_json))

        query = SearchQuery(text=query_text, k=k, filters=filters, rerank=not no_rerank)
        result = await container.search.find_passages(query)

        if json_output:
            console.print(result.model_dump_json(indent=2))
            return

        table = Table(title=f"Search: {query_text}")
        table.add_column("#", style="dim", width=3)
        table.add_column("Score", width=6)
        table.add_column("Document", width=15)
        table.add_column("Text", max_width=80)

        for i, hit in enumerate(result.hits, 1):
            text_preview = hit.text[:200].replace("\n", " ")
            table.add_row(
                str(i),
                f"{hit.score:.3f}",
                str(hit.document_id)[:8],
                text_preview,
            )

        console.print(table)
        console.print(f"\n{result.total_candidates} candidates searched")
    finally:
        await container.close()
