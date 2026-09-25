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
    edition_key: str | None = typer.Option(None, "--edition-key", help="Edition key."),
    claim: str | None = typer.Option(None, "--claim", help="Claim ref."),
) -> None:
    """List the works citing a source. Exactly one selector."""
    asyncio.run(_citations(document, edition_key, claim))


async def _citations(
    document: str | None, edition_key: str | None, claim: str | None
) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.services.works.citations import WorkCitationFinder

    given = [name for name, value in
             (("document", document), ("edition-key", edition_key), ("claim", claim))
             if value is not None]
    if len(given) != 1:
        console.print("[red]Pass exactly one of --document, --edition-key, --claim.[/red]")
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
            document_id=doc_uuid, edition_key=edition_key, claim_ref=claim
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


@work_app.command("cite-entry")
def cite_entry_command(
    document: str = typer.Option(..., "--document", help="Cited document UUID."),
    quote: str = typer.Option(..., "--quote", help="The wording as typed."),
    intent: str = typer.Option(..., "--intent", help="Citation intent."),
    citation_id: str | None = typer.Option(None, "--id", help="Handle, e.g. c1."),
    role: str | None = typer.Option(None, "--role", help="Claim role."),
    edition: str | None = typer.Option(None, "--edition", help="Edition string."),
    edition_key: str | None = typer.Option(None, "--edition-key", help="Edition key."),
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
        _cite(document, quote, intent, citation_id, role, edition, edition_key,
              locator, window, json_output)
    )


async def _cite(
    document: str,
    quote: str,
    intent: str,
    citation_id: str | None,
    role: str | None,
    edition: str | None,
    edition_key: str | None,
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
                edition_key=edition_key,
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


@work_app.command("show")
def show_command(
    slug: str = typer.Argument(..., help="Work slug."),
    revision: int | None = typer.Option(None, "--revision", help="Revision number."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Print a work's block tree with citations inlined."""
    asyncio.run(_show(slug, revision, json_output))


async def _show(slug: str, revision: int | None, json_output: bool) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.domain.errors import NotFoundError

    container = await build_container(load_settings())
    try:
        assert container.work_service is not None  # always built
        try:
            result = await container.work_service.get(slug=slug, revision=revision)
        except NotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if json_output:
            print(json.dumps(result, indent=2))
        else:
            console.print(f"[bold]{result['work']['slug']}[/bold] rev "
                          f"{result['revision']['revision_number']} "
                          f"({result['revision']['state']})")
            for block in result["blocks"]:
                title = f" {block['title']}" if block["title"] else ""
                console.print(f"  [{block['block_type']}]{title} "
                              f"{block['block_key']} pos {block['position']}")
                for citation in block["citations"]:
                    console.print(f"    {{{{cite:{citation['citation_key']}}}}} "
                                  f"{citation['intent']}")
    finally:
        await container.close()


@work_app.command("validate")
def validate_command(
    slug: str = typer.Argument(..., help="Work slug."),
    revision: int | None = typer.Option(None, "--revision", help="Revision number."),
    gate: str = typer.Option("none", "--gate", help="Judge against a gate: none, freeze, publish."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Check a revision's rows and judge them against a gate."""
    asyncio.run(_validate(slug, revision, gate, json_output))


async def _validate(
    slug: str, revision: int | None, gate: str, json_output: bool
) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.domain.errors import NotFoundError

    if gate not in ("none", "freeze", "publish"):
        console.print(f"[red]gate must be none, freeze, or publish, got {gate!r}[/red]")
        raise typer.Exit(code=2)
    container = await build_container(load_settings())
    try:
        assert container.work_validation is not None  # always built
        try:
            report = await container.work_validation.validate(
                slug=slug, revision=revision, gate=gate  # type: ignore[arg-type]
            )
        except NotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
        if json_output:
            print(report.model_dump_json(indent=2))
        else:
            for finding in report.findings:
                colour = {"error": "red", "warning": "yellow"}.get(
                    finding.severity, "dim"
                )
                scope = finding.citation_key or finding.block_key or ""
                console.print(f"[{colour}]{finding.severity} [{scope}]: "
                              f"{finding.rule_id}[/{colour}]")
                console.print(f"  {finding.message}")
            console.print(
                f"gate {report.gate.name}: "
                f"[{'green' if report.gate.passed else 'red'}]"
                f"{'passed' if report.gate.passed else 'FAILED'}[/]"
            )
        if gate != "none" and not report.gate.passed:
            raise typer.Exit(code=1)
    finally:
        await container.close()


@work_app.command("freeze")
def freeze_command(
    slug: str = typer.Argument(..., help="Work slug."),
    message: str | None = typer.Option(None, "--message", help="Why this revision is sealed."),
    waiver: list[str] | None = typer.Option(
        None, "--waiver",
        help="Repeatable 'RULE_ID:subject:reason'. Subject may be empty.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Gate, waive, hash, and seal the current draft revision."""
    asyncio.run(_freeze(slug, message, waiver or [], json_output))


async def _freeze(
    slug: str, message: str | None, waivers: list[str], json_output: bool
) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.domain.errors import NotFoundError
    from research_engine.services.works.publication import FreezeBlocked, WaiverGiven

    given: list[WaiverGiven] = []
    for text in waivers:
        rule_id, _, rest = text.partition(":")
        subject, _, reason = rest.partition(":")
        if not rule_id or not reason:
            console.print(f"[red]--waiver must be 'RULE_ID:subject:reason', got {text!r}[/red]")
            raise typer.Exit(code=2)
        given.append(
            WaiverGiven(rule_id=rule_id, subject=subject or None, reason=reason)
        )
    container = await build_container(load_settings())
    try:
        assert container.work_publication is not None  # always built
        try:
            sealed = await container.work_publication.freeze(
                slug=slug, message=message, waivers=given
            )
        except FreezeBlocked as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        except NotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if json_output:
            print(sealed.model_dump_json(indent=2))
        else:
            console.print(f"[green]frozen rev {sealed.revision_number}[/green] "
                          f"{sealed.content_hash}")
    finally:
        await container.close()


@work_app.command("publish")
def publish_command(
    slug: str = typer.Argument(..., help="Work slug."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Validate at the publish gate, then seal the frozen revision as published."""
    asyncio.run(_publish(slug, json_output))


async def _publish(slug: str, json_output: bool) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.domain.errors import FrozenRevisionError, NotFoundError
    from research_engine.services.works.publication import FreezeBlocked

    container = await build_container(load_settings())
    try:
        assert container.work_publication is not None  # always built
        try:
            sealed = await container.work_publication.publish(slug=slug)
        except FreezeBlocked as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        except (NotFoundError, FrozenRevisionError) as exc:
            # A draft is not publishable: the repo transition refuses it.
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if json_output:
            print(sealed.model_dump_json(indent=2))
        else:
            console.print(f"[green]published rev {sealed.revision_number}[/green] "
                          f"{sealed.content_hash}")
    finally:
        await container.close()


@work_app.command("export")
def export_command(
    slug: str = typer.Argument(..., help="Work slug."),
    draft: bool = typer.Option(False, "--draft", help="Render the current draft revision."),
    revision: int | None = typer.Option(None, "--revision", help="Render a numbered revision (frozen history included)."),
    out: str | None = typer.Option(None, "--out", help="Write to this file."),
) -> None:
    """Render one revision to markdown (no manifest yet)."""
    asyncio.run(_export(slug, draft, revision, out))


async def _export(slug: str, draft: bool, revision: int | None, out: str | None) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.domain.errors import NotFoundError

    if draft == (revision is not None):
        console.print("[red]Pass exactly one of --draft (current) or --revision N.[/red]")
        raise typer.Exit(code=2)
    container = await build_container(load_settings())
    try:
        assert container.work_export is not None  # always built
        try:
            rendered = await container.work_export.export_draft(
                slug=slug, revision=revision
            )
        except NotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if out:
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(rendered)
            console.print(f"Wrote {out}")
        else:
            print(rendered, end="")
    finally:
        await container.close()


@work_app.command("import")
def import_command(
    path: str = typer.Argument(..., help="Edited markdown file."),
    slug: str = typer.Option(..., "--slug", help="Work slug."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute the diff, write nothing."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Apply edited markdown as a new draft revision (copy-forward)."""
    asyncio.run(_import(path, slug, dry_run, json_output))


async def _import(path: str, slug: str, dry_run: bool, json_output: bool) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.domain.errors import NotFoundError
    from research_engine.services.works.drafting import ImportRefused

    try:
        with open(path, encoding="utf-8") as handle:
            markdown = handle.read()
    except OSError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    container = await build_container(load_settings())
    try:
        assert container.work_export is not None  # always built
        try:
            diff = await container.work_export.import_draft(
                slug=slug, markdown=markdown, dry_run=dry_run
            )
        except (NotFoundError, ImportRefused) as exc:
            rule = f"{exc.rule_id}: " if isinstance(exc, ImportRefused) else ""
            console.print(f"[red]{rule}{exc}[/red]")
            raise typer.Exit(code=1) from exc
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
        if json_output:
            print(diff.model_dump_json(indent=2))
        else:
            console.print(f"rev {diff.revision_number}"
                          f"{' (dry run)' if diff.dry_run else ''}: "
                          f"{len(diff.changes)} change(s)")
            for change in diff.changes:
                console.print(f"  {change.change} {change.block_key}")
    finally:
        await container.close()


@work_app.command("promote")
def promote_command(
    path: str = typer.Argument(..., help="Plain markdown note to promote."),
    slug: str = typer.Option(..., "--slug", help="Work slug to create."),
    title: str = typer.Option(..., "--title", help="Work title."),
    work_type: str = typer.Option(..., "--type", help="Work type (e.g. script, essay)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute the diff, write nothing."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Make a plain note a work: create it and land the note as revision 1."""
    asyncio.run(_promote(path, slug, title, work_type, dry_run, json_output))


async def _promote(
    path: str, slug: str, title: str, work_type: str, dry_run: bool, json_output: bool
) -> None:
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.domain.errors import NotFoundError
    from research_engine.services.works.drafting import ImportRefused

    try:
        with open(path, encoding="utf-8") as handle:
            markdown = handle.read()
    except OSError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    container = await build_container(load_settings())
    try:
        assert container.work_export is not None  # always built
        try:
            diff = await container.work_export.promote(
                slug=slug, title=title, work_type=work_type,
                markdown=markdown, dry_run=dry_run,
            )
        except (NotFoundError, ImportRefused) as exc:
            rule = f"{exc.rule_id}: " if isinstance(exc, ImportRefused) else ""
            console.print(f"[red]{rule}{exc}[/red]")
            raise typer.Exit(code=1) from exc
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
        if json_output:
            print(diff.model_dump_json(indent=2))
        else:
            console.print(f"rev {diff.revision_number}"
                          f"{' (dry run)' if diff.dry_run else ''}: "
                          f"{len(diff.changes)} change(s)")
            for change in diff.changes:
                console.print(f"  {change.change} {change.block_key}")
    finally:
        await container.close()


@work_app.command("set-key")
def set_key_command(
    document_id: str = typer.Argument(..., help="Document UUID."),
    edition_key: str = typer.Argument(..., help="Edition key to store on the document."),
) -> None:
    """Store an edition key in a document's metadata for later joins."""
    asyncio.run(_set_key(document_id, edition_key))


async def _set_key(document_id: str, edition_key: str) -> None:
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
        updated = await container.docs.update_metadata(doc_uuid, {"edition_key": edition_key})
        console.print(f"{updated.title or updated.id}: edition_key={edition_key}")
    finally:
        await container.close()
