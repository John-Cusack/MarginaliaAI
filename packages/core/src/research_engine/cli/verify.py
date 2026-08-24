"""`research-engine verify-quote` — check a quotation against its source."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import typer
from rich.console import Console

console = Console()

_TIER_STYLE = {
    "exact": ("green", "The source says precisely this."),
    "normalized": (
        "yellow",
        "The source says this apart from typography. Compare the source text "
        "below before quoting it verbatim.",
    ),
    "near": ("yellow", "Part of this matches. Where it diverges is shown below."),
    "not_found": ("red", "No document contains this."),
    "no_canonical_text": (
        "red",
        "That document has no stored text, so nothing could be checked. This is "
        "not the same as the quotation being absent.",
    ),
}


def verify_quote_command(
    text: str = typer.Argument(..., help="The quotation to check."),
    document_id: str = typer.Option(
        None, "--document-id", "-d", help="Restrict to one document."
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Check a quotation against the corpus and say where it comes from."""
    asyncio.run(_verify(text, document_id, json_output))


async def _verify(text: str, document_id: str | None, json_output: bool) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings

    container = await build_container(load_settings())
    try:
        result = await container.verification.verify(
            text, UUID(document_id) if document_id else None
        )
        if json_output:
            print(result.model_dump_json(indent=2))
            return

        colour, explanation = _TIER_STYLE.get(result.tier.value, ("white", ""))
        console.print(f"\n[{colour}][bold]{result.tier.value}[/bold][/{colour}] — {explanation}")

        if result.location:
            loc = result.location
            console.print(f"\n  {loc.document_title or loc.document_id}")
            console.print(f"  characters {loc.char_start}–{loc.char_end}")
            if loc.locators:
                console.print(f"  locator: {json.dumps(loc.locators[0])}")
            elif loc.passage_ids:
                # Silence here would read as "no page number exists", when the
                # truth is that this document was ingested without one.
                console.print("  locator: none recorded for this document")
            if loc.straddles_passages:
                console.print(
                    f"  spans {len(loc.passage_ids)} passages — searching passage "
                    f"text alone would have missed this"
                )
            console.print(f"\n  source reads: [italic]{loc.source_text}[/italic]")

        if result.divergence:
            d = result.divergence
            console.print(f"\n  matched {d.matched_characters} characters, then:")
            console.print(f"    your quote: [red]{d.quote_continues}[/red]")
            console.print(f"    the source: [green]{d.source_continues}[/green]")
        console.print()
    finally:
        await container.close()
