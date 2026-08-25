"""CLI search commands."""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

console = Console()


# Registered on the root app as a plain command, not a Typer sub-app. As a
# sub-app it was unreachable: a group callback carrying a required Argument
# makes Click demand a subcommand that does not exist, so every invocation died
# with "Missing argument 'QUERY'" no matter what was typed.
def search_command(
    query: str = typer.Argument(..., help="Search query."),
    k: int = typer.Option(10, "--k", "-k", help="Number of results."),
    filters: str | None = typer.Option(None, "--filters", "-f", help="JSON filters."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
    no_rerank: bool = typer.Option(False, "--no-rerank", help="Disable reranking."),
    show_window: bool = typer.Option(
        False, "--window", help="Show the expanded window instead of the matched chunk."
    ),
):
    """Search the corpus with hybrid retrieval."""
    asyncio.run(_search(query, k, filters, json_output, no_rerank, show_window))


async def _search(
    query_text: str,
    k: int,
    filters_json: str | None,
    json_output: bool,
    no_rerank: bool,
    show_window: bool = False,
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
            # Plain stdout, not the rich console. Rich wraps to terminal width
            # and will happily break a line inside a JSON string, so anything
            # piping `--json` into a parser got a syntax error on long text.
            print(result.model_dump_json(indent=2))
            return

        table = Table(title=f"Search: {query_text}")
        table.add_column("#", style="dim", width=3)
        table.add_column("Score", width=6)
        table.add_column("Document", width=15)
        # Narrow, but enough that a regression to no-window is obvious in normal
        # use rather than only in --json.
        table.add_column("Ctx", width=13)
        table.add_column("Window" if show_window else "Text", max_width=80)

        for i, hit in enumerate(result.hits, 1):
            shown = hit.window.text if show_window and hit.window else hit.text
            table.add_row(
                str(i),
                f"{hit.score:.3f}",
                str(hit.document_id)[:8],
                f"{hit.window.source} {hit.window.approx_tokens}t"
                if hit.window
                else "—",
                shown[:200].replace("\n", " "),
            )

        console.print(table)
        console.print(f"\n{result.total_candidates} candidates searched")
        if "rerank_unavailable" in result.degraded:
            console.print(
                "[yellow]Results are not reranked[/yellow] — the rerank "
                "backend did not answer, so these are fused (RRF) rankings. "
                "Ordering is less precise than usual; see the log line above "
                "for whether it was unreachable or merely too slow."
            )
    finally:
        await container.close()
