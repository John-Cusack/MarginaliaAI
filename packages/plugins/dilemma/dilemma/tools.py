"""MCP tools: load a profile, ingest its editions, read coverage and the queue.

Frozen in `plugin.yaml`, so the pack's approval hash survives readers being
added. Run them over MCP, through ``research-engine dilemma …``, or with
``research-engine tool call dilemma_<name> --json '{…}'``.
"""

from __future__ import annotations

from typing import Any

from dilemma import __version__, catalog, pipeline


async def profile_load(bibliography: Any, profiles: Any, key: str) -> dict[str, Any]:
    """Register the profile's editions and store its roles under its data hash."""
    return await pipeline.load_profile(bibliography, profiles, key)


async def profile_show(profiles: Any, key: str, on: str | None = None) -> dict[str, Any]:
    """The profile as stored — hash, settings, roles (optionally those in force on a date)."""
    stored = await profiles.get_profile(key, on=on)
    if stored is None:
        return {"profile": key, "loaded": False}
    declared = catalog.profile(key)
    return {
        **stored,
        "loaded": True,
        "current": stored["profile_hash"] == declared.profile_hash,
    }


async def ingest(
    bibliography: Any,
    ingestion: Any,
    http: Any,
    profiles: Any,
    context: Any,
    profile: str,
    slice: str | None = None,  # noqa: A002 - the manifest's word
    source_key: str | None = None,
    stage_only: bool = False,
    accept_mismatch: bool = False,
) -> dict[str, Any]:
    """Load one edition, or every edition in one of the profile's slices.

    The profile must be loaded and current: a run records the hash of the
    profile data it ran under, so loading against stale roles is refused.
    """
    if bool(slice) == bool(source_key):
        raise ValueError("give exactly one of slice or source_key")
    declared = catalog.profile(profile)
    stored = await profiles.get_profile(profile)
    if stored is None or stored["profile_hash"] != declared.profile_hash:
        raise ValueError(
            f"profile {profile!r} is not loaded, or its data changed since it was; "
            f"run `research-engine dilemma profile load {profile}` first"
        )
    if source_key:
        if source_key not in declared.editions_needed():
            raise ValueError(f"profile {profile!r} names no edition {source_key!r}")
        keys = [source_key]
    else:
        keys = list(declared.slice(slice).sources)

    config = {"slice": slice, "source_key": source_key, "stage_only": stage_only,
              "accept_mismatch": accept_mismatch, "editions": keys}
    run = await profiles.start_run(
        profile, stage="ingest", config=config, code_version=f"dilemma {__version__}",
        items_in=len(keys),
    )
    outcomes: list[dict[str, Any]] = []
    try:
        for key in keys:
            outcome = await pipeline.ingest_source(
                key, bibliography=bibliography, ingestion=ingestion, http=http,
                data_dir=context.data_dir, stage_only=stage_only,
                accept_mismatch=accept_mismatch,
            )
            outcomes.append(outcome.as_dict())
    except BaseException:
        await profiles.finish_run(
            run["id"], status="failed", items_out=_loaded(outcomes),
            stats={"outcomes": outcomes},
        )
        raise
    by_status: dict[str, int] = {}
    for outcome in outcomes:
        by_status[outcome["status"]] = by_status.get(outcome["status"], 0) + 1
    await profiles.finish_run(
        run["id"], items_out=_loaded(outcomes),
        stats={"by_status": by_status, "outcomes": outcomes},
    )
    return {
        "run_id": run["id"],
        "profile": profile,
        "editions": len(keys),
        "by_status": by_status,
        "passages_loaded": sum(o.get("passages", 0) for o in outcomes if o["status"] == "loaded"),
        "coverage_ok": all(o.get("coverage_ok", True) for o in outcomes),
        "awaiting_operator": [
            {"source_key": o["source_key"], "instructions": o.get("detail")}
            for o in outcomes if o["status"] == "awaiting_operator"
        ],
        "blocked": [o for o in outcomes if o["status"] == "blocked"],
        "license_notes": {o["source_key"]: o.get("license") for o in outcomes},
        "outcomes": outcomes,
    }


async def coverage(bibliography: Any, source_key: str) -> dict[str, Any]:
    """One edition's acquisition, artifacts and coverage history."""
    source = await bibliography.get_source(source_key)
    if source is None:
        return {"source_key": source_key, "registered": False}
    return source


async def blocked_queue(bibliography: Any, profile: str | None = None) -> dict[str, Any]:
    """The scan and purchase list, most valuable first.

    Ordered by the best tier any profile (or the one named) gives the edition —
    a missing T1 text costs most — then by group and key. An edition with no
    role of its own takes the best tier of its work: a critical edition of a
    canonical book is worth what the book is worth.
    """
    keys = [profile] if profile else catalog.profile_keys()
    tier: dict[str, int] = {}
    for key in keys:
        for role in catalog.profile(key).spec.roles:
            if role.tier is not None:
                tier[role.edition] = min(role.tier, tier.get(role.edition, role.tier))
    work_tier: dict[str, int] = {}
    for _, known in catalog.entries():
        if known.key in tier:
            work_tier[known.work] = min(tier[known.key], work_tier.get(known.work, 9))
    wanted = (
        set(catalog.profile(profile).editions_needed()) if profile else None
    )
    rows = []
    for source in await bibliography.list_sources(status="blocked"):
        edition_key = source["edition_key"]
        if wanted is not None and edition_key not in wanted:
            continue
        try:
            group, entry = catalog.entry(edition_key)
        except KeyError:
            group, entry = None, None
        rank = tier.get(edition_key) or (work_tier.get(entry.work) if entry else None)
        rows.append({
            "source_key": edition_key,
            "title": (source.get("attributes") or {}).get("title"),
            "group": group,
            "tier": f"T{rank}" if rank else None,
            "reason": source.get("blocked_reason"),
            "suggested_acquisition": source.get("suggested_acquisition"),
        })
    rows.sort(key=lambda r: (r["tier"] or "T9", r["group"] or "~", r["source_key"]))
    return {"count": len(rows), "queue": rows}


def _loaded(outcomes: list[dict[str, Any]]) -> int:
    return sum(1 for o in outcomes if o["status"] == "loaded")
