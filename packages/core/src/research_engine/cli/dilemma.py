"""`research-engine dilemma …`: the Dilemma Finder's commands.

Loading profiles and their editions is the `dilemma` pack's work — the pack
holds the editions catalog, the profiles and the source readers — so these
commands run its tools through the same dispatch the MCP server uses, with
exactly the clients its approved permissions grant. Later stages (claims,
the funnel, review) are core services and will sit beside them here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import random
from collections import Counter

# Path must exist at runtime for typer and cannot move into TYPE_CHECKING.
from pathlib import Path  # noqa: TC003
from typing import Any
from uuid import UUID

import typer

from research_engine.cli.tool import _call

dilemma_app = typer.Typer(no_args_is_help=True)
profile_app = typer.Typer(no_args_is_help=True)
dilemma_app.add_typer(profile_app, name="profile", help="Load and inspect reader profiles.")
extract_app = typer.Typer(no_args_is_help=True)
dilemma_app.add_typer(
    extract_app, name="extract", help="Packeted claim extraction, import, and audit."
)


def _run(tool_id: str, arguments: dict[str, Any], *, wake_gpu: bool = False) -> None:
    given = {k: v for k, v in arguments.items() if v is not None}
    result = asyncio.run(_call(tool_id, given, wake_gpu=wake_gpu))
    typer.echo(json.dumps(result, default=str, indent=2, ensure_ascii=False))


@profile_app.command("load")
def profile_load(key: str = typer.Argument(..., help="Profile key, e.g. lds.")) -> None:
    """Register the profile's editions and store its roles under its data hash."""
    _run("dilemma.profile_load", {"key": key})


@profile_app.command("show")
def profile_show(
    key: str = typer.Argument(..., help="Profile key."),
    on: str | None = typer.Option(None, "--on", help="Only roles in force on this ISO date."),
) -> None:
    """Show the stored profile, and whether it matches the pack's data."""
    _run("dilemma.profile_show", {"key": key, "on": on})


@dilemma_app.command("ingest")
def ingest(
    profile: str = typer.Argument(..., help="Profile whose editions to load."),
    slice_: str | None = typer.Option(None, "--slice", help="One slice of its manifest."),
    source: str | None = typer.Option(None, "--source", help="One edition it names."),
    stage_only: bool = typer.Option(
        False, "--stage-only", help="Acquire, parse and check; write nothing."
    ),
    accept_mismatch: bool = typer.Option(
        False, "--accept-mismatch",
        help="Load even though coverage does not match; recorded as accepted.",
    ),
    wake_gpu: bool = typer.Option(
        True, "--wake-gpu/--no-wake-gpu", help="Start the GPU embed host first."
    ),
) -> None:
    """Load a slice of a profile's editions (or one edition) as one recorded run."""
    if bool(slice_) == bool(source):
        raise typer.BadParameter("give exactly one of --slice or --source")
    _run(
        "dilemma.ingest",
        {"profile": profile, "slice": slice_, "source_key": source,
         "stage_only": stage_only, "accept_mismatch": accept_mismatch},
        wake_gpu=wake_gpu and not stage_only,
    )


@dilemma_app.command("coverage")
def coverage(source: str = typer.Argument(..., help="Catalog edition key.")) -> None:
    """One edition's acquisition, artifacts and coverage history."""
    _run("dilemma.coverage", {"source_key": source})


@dilemma_app.command("blocked")
def blocked(
    profile: str | None = typer.Option(None, "--profile", help="Only this profile's editions."),
) -> None:
    """The scan and purchase queue, highest tier first."""
    _run("dilemma.blocked_queue", {"profile": profile})


# --- Extraction: packets out, answers in, claims materialized ---
#
# These are core services, not pack tools, so they take the container
# directly — the same pattern as `cli/extraction.py`. The packets are run
# outside this process (another model, a batch job); the import holds the
# answers to the executor's own validation, then materializes them as
# machine claims under the run the packets opened.

_COST_GATE_ITEMS = 1000


async def _container():
    from research_engine.composition import build_container
    from research_engine.config import load_settings

    container = await build_container(load_settings())
    return container, container.close


def _print_estimate(estimate: dict[str, Any]) -> None:
    typer.echo(
        f"items: {estimate['items']}  "
        f"est. input tokens: {estimate['input_tokens_est']}  "
        f"est. output tokens: {estimate['output_tokens_est']}  "
        f"est. minutes: {estimate['seconds_est'] / 60:.1f}"
    )


def _parse_strata(stratum: list[str] | None) -> dict[str, int]:
    strata: dict[str, int] = {}
    for given in stratum or []:
        edition, sep, count = given.partition("=")
        if not sep or not edition or not count.isdigit():
            raise typer.BadParameter(
                f"--stratum wants EDITION=N, got {given!r}"
            )
        strata[edition] = int(count)
    if not strata:
        raise typer.BadParameter("give at least one --stratum EDITION=N")
    return strata


@extract_app.command("packets")
def extract_packets(
    profile: str = typer.Option(..., "--profile", help="Profile key."),
    schema: str = typer.Option(..., "--schema", help="Schema as 'name:version'."),
    stratum: list[str] = typer.Option(
        None, "--stratum", help="EDITION=N passages to sample. Repeatable."
    ),
    seed: int = typer.Option(1, "--seed", help="Sampling seed."),
    context_before: int = typer.Option(1, "--context-before"),
    context_after: int = typer.Option(1, "--context-after"),
    entity_hints_file: Path | None = typer.Option(None, "--entity-hints-file"),
    out: Path = typer.Option(..., "--out", help="Packets JSONL to write."),
    model: str = typer.Option(
        ..., "--model", help="Label of the model that will run the packets."
    ),
    yes: bool = typer.Option(False, "--yes", help="Accept a run over the cost gate."),
    seconds_per_item: float = typer.Option(20, "--seconds-per-item"),
) -> None:
    """Sample passages and write extraction packets plus their meta sidecar.

    The cost gate is the point of writing packets before running anything:
    the estimate prints always, and a build over the gate without `--yes`
    starts no run and writes no file.
    """
    strata = _parse_strata(stratum)
    hints = (
        entity_hints_file.read_text(encoding="utf-8") if entity_hints_file else ""
    )
    try:
        result = asyncio.run(
            _packets(
                profile=profile,
                schema_ref=schema,
                strata=strata,
                seed=seed,
                context_before=context_before,
                context_after=context_after,
                entity_hints=hints,
                out=out,
                model=model,
                yes=yes,
                seconds_per_item=seconds_per_item,
            )
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    _print_estimate(result["estimate"])
    if result["gated"]:
        typer.echo(
            f"Refused: {result['estimate']['items']} items is over the "
            f"{_COST_GATE_ITEMS}-item cost gate. Re-run with --yes to accept."
        )
        raise typer.Exit(code=1)
    typer.echo(f"run: {result['run_id']}")
    typer.echo(f"packets: {result['out']}")
    typer.echo(f"meta: {result['meta']}")


async def _packets(
    *,
    profile: str,
    schema_ref: str,
    strata: dict[str, int],
    seed: int,
    context_before: int,
    context_after: int,
    entity_hints: str,
    out: Path,
    model: str,
    yes: bool,
    seconds_per_item: float,
) -> dict[str, Any]:
    from research_engine.services.dilemma.packets import build_packets, estimate
    from research_engine.services.extraction.caching import extractor_version

    container, close = await _container()
    try:
        packets = await build_packets(
            container.engine,
            container.passages,
            container.extraction_schemas,
            profile_key=profile,
            schema_ref=schema_ref,
            strata=strata,
            seed=seed,
            context_before=context_before,
            context_after=context_after,
            entity_hints=entity_hints,
        )
        estimated = estimate(packets, seconds_per_item=seconds_per_item)
        # The gate runs before anything is written or any run is opened: a
        # refused build must leave neither a file nor a dangling run behind.
        if len(packets) > _COST_GATE_ITEMS and not yes:
            return {"estimate": estimated, "gated": True}

        parts = schema_ref.split(":")
        version = int(parts[1]) if len(parts) > 1 and parts[1] else 1
        schema = await container.extraction_schemas.get_by_name_version(
            parts[0], version
        )
        assert schema is not None  # build_packets refused an unknown schema
        hints_hash = hashlib.sha256(entity_hints.encode("utf-8")).hexdigest()
        version_key = extractor_version(
            schema.version,
            schema.prompt_template,
            model,
            schema_def=schema.schema_def,
            context_before=context_before,
            context_after=context_after,
            entity_hints=entity_hints,
        )
        options = {
            "context_before": context_before,
            "context_after": context_after,
            "entity_hints": entity_hints,
        }
        run = await container.profile_client.start_run(
            profile,
            stage="extraction",
            config={
                "schema": schema_ref,
                "strata": strata,
                "seed": seed,
                "context_before": context_before,
                "context_after": context_after,
                "entity_hints_sha256": hints_hash,
                "extractor_version": version_key,
                "model": model,
            },
            model_versions={"extraction": model},
            items_in=len(packets),
        )
        out.write_text(
            "".join(
                json.dumps(packet, ensure_ascii=False, default=str) + "\n"
                for packet in packets
            ),
            encoding="utf-8",
        )
        meta_path = Path(str(out) + ".meta.json")
        meta_path.write_text(
            json.dumps(
                {
                    "run_id": run["id"],
                    "profile": profile,
                    "schema": schema_ref,
                    "model": model,
                    "options": options,
                    "entity_hints_sha256": hints_hash,
                    "extractor_version": version_key,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "estimate": estimated,
            "gated": False,
            "run_id": run["id"],
            "out": str(out),
            "meta": str(meta_path),
        }
    finally:
        await close()


@extract_app.command("import")
def extract_import(
    packets: Path = typer.Option(..., "--packets", help="Packets JSONL (with sidecar)."),
    results: Path = typer.Option(..., "--results", help="Results JSONL."),
) -> None:
    """Import packet answers, then materialize them as claims under the run."""
    try:
        result = asyncio.run(_import(packets=packets, results=results))
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result, default=str, indent=2, ensure_ascii=False))


async def _import(*, packets: Path, results: Path) -> dict[str, Any]:
    from research_engine.domain.extractions import ExtractionOptions
    from research_engine.services.dilemma.materialize import ClaimMaterializer

    meta = json.loads(
        Path(str(packets) + ".meta.json").read_text(encoding="utf-8")
    )
    records_by_passage: dict[UUID, list[dict[str, Any]]] = {}
    for line in results.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        records = [_as_record(record) for record in row.get("records", [])]
        records_by_passage.setdefault(UUID(str(row["passage_id"])), []).extend(
            records
        )

    options_given = meta.get("options") or {}
    options = ExtractionOptions(
        context_before=options_given.get("context_before", 0),
        context_after=options_given.get("context_after", 0),
        entity_hints=options_given.get("entity_hints", ""),
        force_refresh=bool(options_given.get("force_refresh", False)),
    )
    run_id = UUID(str(meta["run_id"]))

    container, close = await _container()
    try:
        try:
            batch = await container.extraction.import_records(
                records_by_passage,
                meta["schema"],
                model=meta["model"],
                options=options,
            )
            materializer = ClaimMaterializer(
                container.extractions,
                container.extraction_schemas,
                container.passages,
                container.spans,
                container.machine_claims,
                container.entities,
                container.transaction_factory,
                documents=container.docs,
            )
            stats = await materializer.materialize(
                run_id=run_id,
                schema_ref=meta["schema"],
                extractor_version=meta.get("extractor_version"),
            )
            failed = sum(
                1
                for result in batch.results
                if getattr(result.status, "value", str(result.status)) != "ok"
            )
            stats = {
                **stats,
                "extraction_results": len(batch.results),
                "extraction_failed": failed,
            }
            await container.profile_client.finish_run(
                run_id,
                status="finished",
                items_out=stats["claims_created"],
                stats=stats,
            )
        except Exception:
            # The original failure is the story; closing the run is best-effort.
            with contextlib.suppress(Exception):
                await container.profile_client.finish_run(
                    run_id, status="failed"
                )
            raise
        return {"run_id": str(run_id), **stats}
    finally:
        await close()


def _as_record(record: dict[str, Any]) -> dict[str, Any]:
    """One answer record in the executor's `{record_type, fields}` shape.

    A runner answering the packets' output schema already returns that shape;
    a flat `{record_type, <fields...>}` answer is folded into it rather than
    refused on a wrapping detail.
    """
    if isinstance(record.get("fields"), dict):
        return record
    fields = {k: v for k, v in record.items() if k != "record_type"}
    return {"record_type": record.get("record_type"), "fields": fields}


@extract_app.command("audit")
def extract_audit(
    run: str = typer.Option(..., "--run", help="Run id from `extract packets`."),
    out: Path = typer.Option(..., "--out", help="Markdown audit file to write."),
    sample: int | None = typer.Option(
        None, "--sample", help="Audit only this many claims (fixed-seed sample)."
    ),
) -> None:
    """Render a human audit sheet for one run's claims.

    The sheet quotes source text: point `--out` outside the repository.
    Verdict fields are left blank for the auditor to fill in.
    """
    try:
        result = asyncio.run(_audit(run_id=UUID(run), out=out, sample=sample))
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"claims: {result['claims']}  audited: {result['audited']}")
    typer.echo(f"audit: {out}")


async def _audit(
    *, run_id: UUID, out: Path, sample: int | None
) -> dict[str, Any]:
    import sqlalchemy as sa

    container, close = await _container()
    try:
        run_row = await container.runs.get(run_id)
        if run_row is None:
            raise ValueError(f"run not found: {run_id}")
        claims = await container.machine_claims.for_run(run_id)
        vouches = await container.machine_claims.vouches_for_run(run_id)
        async with container.engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT mc.id AS claim_id, er.passage_id, p.locator "
                        "FROM dilemma.claims mc "
                        "LEFT JOIN core.extraction_records er "
                        "ON er.id = mc.extraction_record_id "
                        "LEFT JOIN core.passages p ON p.id = er.passage_id "
                        "WHERE mc.run_id = :run_id"
                    ),
                    {"run_id": run_id},
                )
            ).all()
        locator_by_claim = {row.claim_id: row.locator for row in rows}
        passages = {row.passage_id for row in rows if row.passage_id}

        audited = claims
        if sample is not None and 0 <= sample < len(claims):
            chosen = random.Random(0).sample(range(len(claims)), sample)
            audited = [claims[index] for index in sorted(chosen)]

        evidence = await container.machine_claims.evidence_for_claims(
            [claim.id for claim in audited]
        )
        span_text: dict[UUID, str] = {}
        for rows_for_claim in evidence.values():
            for row in rows_for_claim:
                if row.source_span_id not in span_text:
                    span = await container.spans.get(row.source_span_id)
                    span_text[row.source_span_id] = span.quoted_text if span else ""

        modalities = Counter(claim.modality.value for claim in claims)
        # Per passage over what the run was fed (its packets), falling back
        # to the passages its claims actually came from.
        fed = run_row.items_in or len(passages)
        per_passage = len(claims) / fed if fed else 0.0
        lines = [
            f"# Extraction audit — run {run_id}",
            "",
            f"- claims: {len(claims)}",
            f"- vouches: {len(vouches)}",
            f"- passages fed: {fed} ({len(passages)} with claims)",
            f"- claims per passage: {per_passage:.2f}",
            "- modalities: "
            + ", ".join(f"{name}={count}" for name, count in sorted(modalities.items())),
            "- speakers labelled but unresolved: "
            f"{sum(1 for c in claims if c.speaker_label and not c.speaker_entity_id)}",
            "- subjects labelled but unresolved: "
            f"{sum(1 for c in claims if c.subject_label and not c.subject_entity_id)}",
            f"- audited below: {len(audited)}",
            "",
        ]
        for claim in audited:
            lines += [
                f"## Claim {claim.id}",
                "",
                f"- statement: {claim.statement}",
                f"- speaker: {claim.speaker_label or '—'}"
                + (f" ({claim.speaker_ref})" if claim.speaker_ref else ""),
                f"- subject: {claim.subject_label or '—'}"
                + (f" ({claim.subject_ref})" if claim.subject_ref else ""),
                f"- modality: {claim.modality.value}",
                f"- polarity: {claim.polarity.value}",
                f"- confidence: {claim.confidence}",
                f"- locator: {json.dumps(locator_by_claim.get(claim.id), default=str)}",
                "- evidence:",
            ]
            for row in evidence.get(claim.id, []):
                lines.append(f"  > {span_text.get(row.source_span_id, '')}")
            lines += [
                "",
                "verdict: __  speaker_ok: __  modality_ok: __  notes: __",
                "",
            ]
        out.write_text("\n".join(lines), encoding="utf-8")
        return {"claims": len(claims), "audited": len(audited)}
    finally:
        await close()
