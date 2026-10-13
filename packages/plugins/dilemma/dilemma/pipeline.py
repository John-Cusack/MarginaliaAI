"""Load a profile, and take one edition from its source to the corpus.

Loading a profile **registers** every edition it names — terms, work, and, for
a source known to be unobtainable, `blocked` with the way to get it — and then
stores the profile's roles, whole, under the hash of its data.

Every edition then goes through the same steps, whoever wrote its reader:

1. **acquire** its raw artifacts — through the polite fetcher, or from the
   operator's inbox for a site whose terms forbid automated access — each
   recorded by a digest core computes;
2. **parse** them into units, verbatim;
3. **check** the units against the edition's known shape and **dump** a QA
   sample, before anything is written, so a bad parse is seen, not stored;
4. **refuse** a load whose counts do not match, unless the operator accepts
   the mismatch by name — a partial load is never written silently;
5. **ingest** each document with its tree, windows and marks, under the
   reader's parser identity;
6. **report** coverage and mark the edition `loaded`.

A loaded edition is not loaded again: superseding a text is a deliberate act
this pipeline does not yet perform, so re-running a slice never doubles a
document. A robots.txt refusal blocks the source rather than failing it: the
honest next step is permission, not a workaround.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dilemma import __version__, catalog
from dilemma.fetch import PoliteFetcher, RobotsDisallowed
from dilemma.ingest import coverage, qa
from dilemma.sources import AcquireContext, AwaitingOperator, adapter_for
from research_engine_sdk.units import layout

if TYPE_CHECKING:
    from dilemma.catalog import EditionEntry, LoadedProfile

#: Attribute marking a block the catalog declared, which only it may lift.
BLOCKED_BY_CATALOG = "blocked_by_catalog"


@dataclass
class IngestOutcome:
    """The per-edition summary a load reports back."""

    source_key: str
    status: str
    documents: int = 0
    documents_already_present: int = 0
    passages: int = 0
    coverage_ok: bool | None = None
    coverage: list[dict[str, Any]] = field(default_factory=list)
    qa_path: str | None = None
    blocked: dict[str, Any] | None = None
    license: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], {})}


async def register_entry(bibliography: Any, group: str, entry: EditionEntry) -> dict[str, Any]:
    """Declare one catalog entry to core, without disturbing its progress."""
    current = await bibliography.get_source(entry.key)
    declared = {
        "work_key": entry.work,
        "collection": entry.collection,
        "method": entry.acquire.method,
        "url": entry.acquire.url,
        "license": entry.acquire.license,
        "license_notes": entry.acquire.notes,
        "manifest_ref": f"pack:dilemma/editions/{group}#{entry.key}",
        "attributes": _attributes(group, entry),
    }
    # An edition that predates this pack keeps the citation data it has.
    csl = None if entry.existing else _csl(entry)
    status = current["status"] if current else None
    if entry.blocked and status in (None, "planned", "blocked"):
        return await bibliography.mark_blocked(
            entry.key, reason=entry.blocked.reason,
            suggested_acquisition=entry.blocked.suggest, csl=csl, **declared,
        )
    if entry.existing and status != "loaded":
        return await bibliography.register_source(
            entry.key, status="loaded", csl=csl, **declared
        )
    result = await bibliography.register_source(entry.key, csl=csl, **declared)
    # The catalog blocked this source and no longer does — an API key arrived,
    # a scan was made. A block the pipeline found for itself (robots.txt) is
    # not the catalog's to lift.
    if status == "blocked" and (current.get("attributes") or {}).get(BLOCKED_BY_CATALOG):
        result = await bibliography.set_status(entry.key, "planned")
    return result


async def load_profile(bibliography: Any, profiles: Any, key: str) -> dict[str, Any]:
    """Register every edition the profile names, then store its roles whole."""
    loaded = catalog.profile(key)
    statuses: dict[str, int] = {}
    for edition_key in loaded.editions_needed():
        group, entry = catalog.entry(edition_key)
        state = await register_entry(bibliography, group, entry)
        statuses[state["status"]] = statuses.get(state["status"], 0) + 1
    stored = await profiles.load_profile(
        key,
        version=loaded.spec.version,
        profile_hash=loaded.profile_hash,
        roles=[role.as_client_role() for role in loaded.spec.roles],
        title=loaded.spec.title,
        settings=_settings(loaded),
    )
    return {
        "profile": key,
        "version": loaded.spec.version,
        "profile_hash": loaded.profile_hash,
        "roles": stored["roles"],
        "editions": sum(statuses.values()),
        "editions_by_status": statuses,
        "files": list(loaded.files),
    }


async def ingest_source(
    key: str,
    *,
    bibliography: Any,
    ingestion: Any,
    http: Any,
    data_dir: Path,
    stage_only: bool = False,
    accept_mismatch: bool = False,
    fetcher: PoliteFetcher | None = None,
) -> IngestOutcome:
    group, entry = catalog.entry(key)
    outcome = IngestOutcome(source_key=key, status="planned", license=entry.acquire.license)
    state = await register_entry(bibliography, group, entry)
    if entry.existing:
        outcome.status, outcome.detail = "loaded", "already in the corpus; registered only"
        return outcome
    if state["status"] == "blocked":
        outcome.status = "blocked"
        outcome.blocked = {
            "reason": state.get("blocked_reason"),
            "suggested_acquisition": state.get("suggested_acquisition"),
        }
        return outcome
    if state["status"] == "loaded" and not stage_only:
        outcome.status = "loaded"
        outcome.detail = (
            "already loaded; not loaded again (superseding a loaded text is not "
            "supported yet)"
        )
        return outcome

    adapter = adapter_for(key)
    if adapter is None:
        outcome.detail = "no reader registered for this edition yet"
        return outcome

    fetcher = fetcher or PoliteFetcher(http, data_dir)
    context = AcquireContext(entry=entry, fetcher=fetcher, inbox=Path(data_dir) / "inbox" / key)
    try:
        artifacts = await adapter.acquire(context)
    except AwaitingOperator as exc:
        outcome.status = "awaiting_operator"
        outcome.detail = str(exc)
        return outcome
    except RobotsDisallowed as exc:
        await bibliography.mark_blocked(
            key, reason=f"robots.txt disallows fetching: {exc}", suggested_acquisition="permission"
        )
        outcome.status = "blocked"
        outcome.blocked = {"reason": str(exc), "suggested_acquisition": "permission"}
        return outcome
    for stored in artifacts:
        await bibliography.attach_artifact(
            key, stored.path, sha256=stored.sha256, url=stored.url,
            retrieved_at=stored.retrieved_at.isoformat(),
        )
    if state["status"] != "loaded":
        await bibliography.set_status(key, "acquired")

    documents = list(adapter.parse(key, artifacts))
    layouts = [layout(document) for document in documents]
    counts = coverage.check(entry, documents)
    outcome.qa_path = str(qa.dump(data_dir, entry, documents, parser=adapter.parser))
    outcome.documents = len(documents)
    outcome.passages = sum(len(laid.passages) for laid in layouts)
    outcome.coverage = [
        {"unit": c.unit, "loaded": c.loaded, "expected": c.expected,
         "anomalies": c.anomalies, "ok": c.ok}
        for c in counts
    ]
    outcome.coverage_ok = all(c.ok for c in counts)
    if stage_only:
        outcome.status = "staged"
        return outcome
    if not outcome.coverage_ok and not accept_mismatch:
        # Recorded, so the refusal is visible in the edition's history; nothing
        # else is written.
        await _report(bibliography, key, counts, accepted=False)
        outcome.status = "refused"
        outcome.detail = (
            "coverage does not match the edition's known shape; nothing was "
            "written. Fix the reader, or re-run with accept_mismatch to load it "
            "as it is."
        )
        return outcome

    for document, laid in zip(documents, layouts, strict=True):
        result = await ingestion.ingest_drafts(
            document.title,
            document.document_type,
            laid.passages,
            source=document.source,
            metadata={**document.metadata, "edition_key": key, "work": entry.work},
            language=document.language,
            full_text=laid.full_text,
            node_drafts=laid.nodes,
            parser=adapter.parser,
            parser_version=adapter.parser_version,
            mark_drafts=laid.marks,
        )
        # The same text from the same source is already stored: an earlier
        # run was interrupted after writing it. It counts as present.
        if result.get("skipped") == "duplicate":
            outcome.documents_already_present += 1
    await _report(bibliography, key, counts, accepted=accept_mismatch and not outcome.coverage_ok)
    await bibliography.set_status(key, "loaded")
    outcome.status = "loaded"
    return outcome


async def _report(bibliography: Any, key: str, counts: list[Any], *, accepted: bool) -> None:
    for count in counts:
        anomalies = list(count.anomalies)
        if accepted and not count.ok:
            anomalies.append("mismatch accepted by the operator at load")
        await bibliography.report_coverage(
            key, unit=count.unit, loaded_units=count.loaded, expected_units=count.expected,
            anomalies=anomalies, checker=coverage.CHECKER,
            checker_version=coverage.CHECKER_VERSION,
        )


def _settings(loaded: LoadedProfile) -> dict[str, Any]:
    return {
        "blind": loaded.spec.blind,
        "internal_tiers": list(loaded.spec.internal_tiers),
        "slices": [part.name for part in loaded.manifest.slices],
        "pack_version": __version__,
    }


def _attributes(group: str, entry: EditionEntry) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "group": group,
            "title": entry.title,
            "author": entry.author,
            "edition": entry.edition,
            "translator": entry.translator,
            "date_composed": entry.date_composed,
            "date_published": entry.date_published,
            "language": entry.language,
            "optional": entry.optional or None,
            "expect": entry.expect or None,
            "select": entry.select or None,
            "verify": list(entry.verify) or None,
            "notes": entry.notes,
            BLOCKED_BY_CATALOG: True if entry.blocked else None,
        }.items()
        if value is not None
    }


def _csl(entry: EditionEntry) -> dict[str, Any]:
    csl: dict[str, Any] = {"type": "book", "title": entry.title, "language": entry.language}
    if entry.author:
        csl["author"] = [{"literal": entry.author}]
    if entry.translator:
        csl["translator"] = [{"literal": entry.translator}]
    if entry.edition:
        csl["edition"] = entry.edition
    if entry.date_published:
        csl["issued"] = {"raw": entry.date_published}
    return csl
