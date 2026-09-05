"""`research-engine work` — verify, cite, render, and key created works."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from research_engine.services.works.verify import VerifyOutput

work_app = typer.Typer(no_args_is_help=True)
console = Console()


@work_app.command("verify")
def verify_command(
    path: str | None = typer.Argument(
        None, help="Work file relative to RE_WORKS_DIR. Omit for all works."
    ),
    gate: str | None = typer.Option(
        None, "--gate", help="Judge against a gate: review or publish."
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Check a work file's citations against the corpus."""
    asyncio.run(_verify(path, gate, json_output))


async def _verify(path: str | None, gate: str | None, json_output: bool) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.services.works.files import WorkFileError
    from research_engine.services.works.verify import VerifyOutput

    gate_name = gate or "none"
    if gate_name not in ("none", "review", "publish"):
        console.print(f"[red]gate must be review or publish, got {gate!r}[/red]")
        raise typer.Exit(code=2)
    container = await build_container(load_settings())
    try:
        if container.work_verifier is None:
            console.print("[red]RE_WORKS_DIR is not set, so no work file can be read.[/red]")
            raise typer.Exit(code=1)
        if path is None:
            output = await container.work_verifier.verify_all(gate_name)  # type: ignore[arg-type]
        else:
            try:
                report = await container.work_verifier.verify_work(path, gate_name)  # type: ignore[arg-type]
            except WorkFileError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
            output = VerifyOutput(
                works=[report],
                summary={
                    "works": 1,
                    "citations": len(report.citations),
                    "errors": sum(1 for f in report.findings if f.severity == "error"),
                    "warnings": sum(1 for f in report.findings if f.severity == "warning"),
                },
            )
        if json_output:
            # Plain stdout, not the rich console: rich wraps to terminal width
            # and breaks long JSON strings mid-line.
            print(output.model_dump_json(indent=2))
        else:
            _print_verify(output)
        if gate is not None and any(
            not work.gate.passed for work in output.works
        ):
            raise typer.Exit(code=1)
    finally:
        await container.close()


def _print_verify(output: VerifyOutput) -> None:
    for work in output.works:
        table = Table(title=f"{work.work_path} — {work.status}")
        table.add_column("Citation", width=8)
        table.add_column("Tier", width=10)
        table.add_column("Span", width=16)
        table.add_column("Findings")
        for citation in work.citations:
            table.add_row(
                citation.id,
                citation.tier or "—",
                f"{citation.char_start}–{citation.char_end}",
                ", ".join(citation.findings) or "ok",
            )
        console.print(table)
        for finding in work.findings:
            colour = {"error": "red", "warning": "yellow", "info": "dim"}[finding.severity]
            scope = f" [{finding.citation_id}]" if finding.citation_id else ""
            console.print(f"[{colour}]{finding.severity}{scope}: {finding.rule_id}[/{colour}]")
            console.print(f"  {finding.message}")
        console.print(
            f"gate {work.gate.name}: "
            f"[{'green' if work.gate.passed else 'red'}]"
            f"{'passed' if work.gate.passed else 'FAILED'}[/]"
        )


@work_app.command("citations")
def citations_command(
    document: str | None = typer.Option(None, "--document", help="Document UUID."),
    zotero: str | None = typer.Option(None, "--zotero", help="Zotero key."),
    claim: str | None = typer.Option(None, "--claim", help="Claim ref."),
) -> None:
    """List the works citing a source. Exactly one selector."""
    asyncio.run(_citations(document, zotero, claim))


async def _citations(
    document: str | None, zotero: str | None, claim: str | None
) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.services.works.citations import WorkCitationFinder

    given = [name for name, value in
             (("document", document), ("zotero", zotero), ("claim", claim))
             if value is not None]
    if len(given) != 1:
        console.print("[red]Pass exactly one of --document, --zotero, --claim.[/red]")
        raise typer.Exit(code=2)
    doc_uuid = None
    if document is not None:
        try:
            doc_uuid = UUID(document)
        except ValueError:
            console.print(f"[red]--document is not a UUID: {document}[/red]")
            raise typer.Exit(code=2) from None
    container = await build_container(load_settings())
    try:
        if container.work_files is None:
            console.print("[red]RE_WORKS_DIR is not set, so no work file can be read.[/red]")
            raise typer.Exit(code=1)
        result = await WorkCitationFinder(container.work_files.works_dir).find(
            document_id=doc_uuid, zotero_key=zotero, claim_ref=claim
        )
        print(json.dumps(result, indent=2))
    finally:
        await container.close()


@work_app.command("render")
def render_command(
    path: str = typer.Argument(..., help="Work file relative to RE_WORKS_DIR."),
    out: str | None = typer.Option(None, "--out", help="Write to this file."),
) -> None:
    """Render a work's body with footnotes derived from the corpus."""
    asyncio.run(_render(path, out))


async def _render(path: str, out: str | None) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.services.works.files import WorkFileError

    container = await build_container(load_settings())
    try:
        if container.work_renderer is None:
            console.print("[red]RE_WORKS_DIR is not set, so no work file can be read.[/red]")
            raise typer.Exit(code=1)
        try:
            result = await container.work_renderer.render(path)
        except WorkFileError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if out:
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(result["rendered"])
            console.print(f"Wrote {out}")
        else:
            print(result["rendered"])
    finally:
        await container.close()


@work_app.command("cite")
def cite_command(
    document: str = typer.Option(..., "--document", help="Cited document UUID."),
    quote: str = typer.Option(..., "--quote", help="The wording as typed."),
    intent: str = typer.Option(..., "--intent", help="Citation intent."),
    citation_id: str | None = typer.Option(None, "--id", help="Handle, e.g. c1."),
    role: str | None = typer.Option(None, "--role", help="Claim role."),
    edition: str | None = typer.Option(None, "--edition", help="Edition string."),
    zotero: str | None = typer.Option(None, "--zotero", help="Zotero key."),
    locator: str | None = typer.Option(
        None, "--locator", help='JSON object, e.g. \'{"page": 214}\'.'
    ),
    window: str | None = typer.Option(
        None, "--window", help="Pin a repeated quote: 'char_start,char_end'."
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Verify a quote, resolve its span, and print a paste-ready entry."""
    asyncio.run(
        _cite(document, quote, intent, citation_id, role, edition, zotero,
              locator, window, json_output)
    )


async def _cite(
    document: str,
    quote: str,
    intent: str,
    citation_id: str | None,
    role: str | None,
    edition: str | None,
    zotero: str | None,
    locator: str | None,
    window: str | None,
    json_output: bool,
) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.services.works.cite import QuoteUnverifiedError

    try:
        doc_uuid = UUID(document)
    except ValueError:
        console.print(f"[red]--document is not a UUID: {document}[/red]")
        raise typer.Exit(code=2) from None
    window_tuple = None
    if window is not None:
        try:
            start_text, end_text = window.split(",")
            window_tuple = (int(start_text), int(end_text))
        except ValueError:
            window_tuple = None
        if window_tuple is None or window_tuple[0] < 0 or window_tuple[1] <= window_tuple[0]:
            console.print("[red]--window must be 'char_start,char_end'.[/red]")
            raise typer.Exit(code=2)
    locator_obj = None
    if locator is not None:
        try:
            locator_obj = json.loads(locator)
        except json.JSONDecodeError:
            locator_obj = None
        if not isinstance(locator_obj, dict):
            console.print("[red]--locator must be a JSON object.[/red]")
            raise typer.Exit(code=2)
    container = await build_container(load_settings())
    try:
        assert container.work_citer is not None  # always built, no works dir needed
        try:
            result = await container.work_citer.cite(
                document_id=doc_uuid,
                quoted_text=quote,
                intent=intent,
                citation_id=citation_id,
                role=role,
                edition=edition,
                zotero_key=zotero,
                locator=locator_obj,
                window=window_tuple,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
        except QuoteUnverifiedError as exc:
            console.print(f"[red]{exc.detail}[/red]")
            raise typer.Exit(code=1) from exc
        if json_output:
            print(result.model_dump_json(indent=2))
        else:
            console.print(f"[green]verified {result.tier}[/green] "
                          f"{result.verified_span[0]}–{result.verified_span[1]}")
            print(result.entry_yaml, end="")
    finally:
        await container.close()


@work_app.command("set-key")
def set_key_command(
    document_id: str = typer.Argument(..., help="Document UUID."),
    zotero_key: str = typer.Argument(..., help="Zotero key to store on the document."),
) -> None:
    """Store a Zotero key in a document's metadata for later joins."""
    asyncio.run(_set_key(document_id, zotero_key))


async def _set_key(document_id: str, zotero_key: str) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings

    try:
        doc_uuid = UUID(document_id)
    except ValueError:
        console.print(f"[red]Not a UUID: {document_id}[/red]")
        raise typer.Exit(code=2) from None
    container = await build_container(load_settings())
    try:
        document = await container.docs.get(doc_uuid)
        if document is None:
            console.print(f"[red]Document not found: {document_id}[/red]")
            raise typer.Exit(code=1)
        updated = await container.docs.update_metadata(doc_uuid, {"zotero_key": zotero_key})
        console.print(f"{updated.title or updated.id}: zotero_key={zotero_key}")
    finally:
        await container.close()
