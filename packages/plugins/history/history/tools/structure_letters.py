"""Split a collected volume of letters into dated letter documents.

``history.structure_letters`` reads the `letter_opening` records extracted from
a `letter_collection` volume, cuts the volume's canonical text into one span per
letter, dates each letter under the rules in `history.letters.dating`, and — in
apply mode — stores each span as a `letter` document with its own date, sender,
recipient and place, plus one `letter_sent` event whose actors carry direction.

Modes:

``structure`` (default)
    A dry run unless ``dry_run`` is false. Re-running with the same inputs
    changes nothing: each letter's source is its volume and offset, so ingest
    finds the document it made last time, and events are keyed on their letter.
``review_queue``
    The held letters — undated, conflicting, hallucinated — with the reason,
    the line as printed, the candidate date and the neighbouring dates. Accept
    one by re-running ``structure`` with ``accept``.
``configure``
    Mark a document as a `letter_collection` and set what this pass reads from
    its metadata: ``default_sender``, ``excluded_ranges``, ``alias_map``,
    ``chronology_tolerance_days``, ``editor``, ``volume``, ``date_span``.

Held letters still become documents — undated, ``review_status:
needs_review`` — so the queue persists between runs. They have no event, and no
date for search or the enricher to trust.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from history.letters import dating, openings, units
from research_engine_sdk import EventFilter

if TYPE_CHECKING:
    from history.letters.dating import Day

COLLECTION_TYPE = "letter_collection"
LETTER_TYPE = "letter"
RECORD_TYPE = "letter_opening"

#: The volume metadata `configure` may set, with the type each must have.
CONFIG_KEYS: dict[str, type | tuple[type, ...]] = {
    "default_sender": (str, dict),
    "excluded_ranges": list,
    "alias_map": dict,
    "chronology_tolerance_days": int,
    "editor": str,
    "volume": (str, int),
    "date_span": list,
}

#: Records read per volume. Faraday vol 1 is 472 passages and ~90 letters.
MAX_RECORDS = 100_000


async def tool_handler(
    corpus: Any,
    extraction: Any,
    event: Any,
    ingestion: Any,
    volume_id: str | None = None,
    mode: str = "structure",
    dry_run: bool = True,
    accept: list[dict[str, Any]] | None = None,
    chronology_tolerance_days: int | None = None,
    strict_single_reader: bool = False,
    config: dict[str, Any] | None = None,
    schema: str = units.SCHEMA,
) -> dict[str, Any]:
    if mode == "review_queue":
        return await _review_queue(corpus, volume_id)
    if not volume_id:
        return {"error": "volume_id is required for this mode."}
    if mode == "configure":
        return await _configure(corpus, ingestion, volume_id, config or {}, dry_run)
    if mode != "structure":
        return {"error": f"unknown mode {mode!r}; use structure, review_queue or configure."}
    return await _structure(
        corpus,
        extraction,
        event,
        ingestion,
        volume_id=volume_id,
        dry_run=dry_run,
        accept=accept or [],
        tolerance=chronology_tolerance_days,
        strict_single_reader=strict_single_reader,
        schema=schema,
    )


async def _structure(
    corpus: Any,
    extraction: Any,
    event: Any,
    ingestion: Any,
    *,
    volume_id: str,
    dry_run: bool,
    accept: list[dict[str, Any]],
    tolerance: int | None,
    strict_single_reader: bool,
    schema: str,
) -> dict[str, Any]:
    volume = await corpus.get_document(volume_id)
    if volume is None:
        return {"error": f"no document {volume_id}."}
    if volume.get("document_type") != COLLECTION_TYPE:
        return {
            "error": (
                f"document {volume_id} is a {volume.get('document_type')!r}, not a "
                f"{COLLECTION_TYPE!r}. Run this tool with mode='configure' to mark it "
                f"one and set its excluded ranges, alias map and default sender."
            )
        }
    meta = volume.get("metadata") or {}
    excluded = [(int(lo), int(hi)) for lo, hi in meta.get("excluded_ranges") or []]
    if tolerance is None and meta.get("chronology_tolerance_days") is not None:
        tolerance = int(meta["chronology_tolerance_days"])

    text = await corpus.get_document_text(volume_id)
    if not text:
        return {"error": f"document {volume_id} has no canonical text to cut letters from."}
    passages = {str(p["id"]): p for p in volume.get("passages") or []}
    records = await extraction.query_records(
        RECORD_TYPE, passage_ids=list(passages), schema=schema, k=MAX_RECORDS
    )
    notes: list[str] = []
    if not records:
        notes.append(
            f"No {RECORD_TYPE} records for this volume under {schema}. Run "
            f"`extract` with that schema over the volume's passages first, leaving "
            f"out its excluded ranges."
        )

    placement = openings.place(records, passages, text, excluded)
    cuts = units.cut_points(await corpus.get_document_outline(volume_id), excluded)
    letter_spans = units.spans(placement.openings, text, cuts)
    empty = [i for i, (start, end) in enumerate(letter_spans) if end <= start]
    for index in reversed(empty):
        placement.misplaced.append(
            {
                "record_id": placement.openings[index].record_ids[0],
                "why": "a chapter or excluded range starts at the opening itself",
                "opening": placement.openings[index].get("opening"),
            }
        )
        del placement.openings[index]
        del letter_spans[index]
    existing = await _existing_letters(ingestion, volume_id)
    reviewed, accept_errors = _reviewed_dates(placement.openings, existing, accept)

    readings = [dating.Reading.of(o.data) for o in placement.openings]
    decisions = dating.decide(
        readings,
        tolerance_days=tolerance,
        strict_single_reader=strict_single_reader,
        reviewed=reviewed,
    )
    letters = []
    for index, (opening, (start, end), decision) in enumerate(
        zip(placement.openings, letter_spans, decisions, strict=True)
    ):
        sender, recipient, flags = units.resolve_actors(
            opening,
            alias_map=meta.get("alias_map"),
            default_sender=meta.get("default_sender"),
        )
        letters.append(
            units.Unit(
                index=index,
                opening=opening,
                start=start,
                end=end,
                decision=decision,
                sender=sender,
                recipient=recipient,
                flags=flags,
            )
        )

    if tolerance is None:
        notes.append(
            "No chronology tolerance was given, so no letter was held for sitting far "
            "from its neighbours; each reports its deviation in days. Applying needs "
            "one: pass chronology_tolerance_days or set it in the volume's metadata."
        )
    report = _report(
        volume_id=volume_id,
        schema=schema,
        dry_run=dry_run,
        tolerance=tolerance,
        records=len(records),
        placement=placement,
        letters=letters,
        existing=existing,
        notes=notes + accept_errors,
    )
    if dry_run:
        return report
    if tolerance is None:
        report["error"] = "refusing to apply without a chronology tolerance."
        return report

    neighbours = _neighbours(letters)
    kept: set[str] = set()
    for unit in letters:
        try:
            outcome = await _apply_unit(
                corpus,
                event,
                ingestion,
                unit,
                text=text,
                volume=volume,
                volume_id=volume_id,
                unit_count=len(letters),
                neighbours=neighbours[unit.index],
                existing=existing.get(units.source_for(volume_id, unit.start), []),
            )
        except Exception as exc:  # noqa: BLE001 - stop, report, and leave a resumable state
            report["error"] = (
                f"stopped at letter {unit.index + 1} of {len(letters)} "
                f"(offset {unit.start}): {type(exc).__name__}: {exc}. Letters before "
                f"it were written; nothing was superseded. Re-running resumes."
            )
            return report
        kept.add(outcome["letter_document_id"])
        report["letters"][unit.index].update(outcome)

    report["superseded"] = await _supersede(event, ingestion, existing, kept)
    return report


async def _apply_unit(
    corpus: Any,
    event: Any,
    ingestion: Any,
    unit: units.Unit,
    *,
    text: str,
    volume: dict[str, Any],
    volume_id: str,
    unit_count: int,
    neighbours: dict[str, Any],
    existing: list[dict[str, Any]],
) -> dict[str, Any]:
    metadata = units.letter_metadata(unit, volume_id=volume_id, unit_count=unit_count)
    metadata["neighbours"] = neighbours
    if "reviewed" in unit.decision.flags and unit.decision.day is not None:
        metadata["reviewer_date"] = unit.decision.day.iso()
    dates = units.document_dates(unit.decision)
    title = units.title_for(unit)
    stored = await ingestion.ingest_document(
        title=title,
        document_type=LETTER_TYPE,
        text=text[unit.start : unit.end],
        source=units.source_for(volume_id, unit.start),
        metadata=metadata,
        language=volume.get("language"),
        edition_id=volume.get("edition_id"),
        **dates,
    )
    letter_id = str(stored["document_id"])
    if stored.get("skipped") == "duplicate":
        await ingestion.update_document(
            letter_id,
            title=title,
            metadata=metadata,
            clear_created_date=not dates,
            **dates,
        )

    letter = await corpus.get_document(letter_id) or {}
    first = min(letter.get("passages") or [], key=lambda p: p["position"], default=None)
    stale = await _events_for(event, letter_id)
    outcome: dict[str, Any] = {"letter_document_id": letter_id, "event_id": None}
    if unit.decision.materialized:
        if first is None:
            raise RuntimeError(f"letter document {letter_id} has no passages to anchor its event")
        written = await event.upsert(
            units.event_for(
                unit,
                letter_document_id=letter_id,
                source_passage_id=str(first["id"]),
                volume_id=volume_id,
            )
        )
        outcome["event_id"] = str(written.id)
        stale = [e for e in stale if str(e.id) != str(written.id)]
    for old in stale:
        await event.delete(old.id)
    return outcome


async def _existing_letters(ingestion: Any, volume_id: str) -> dict[str, list[dict[str, Any]]]:
    """Letter documents an earlier run made from this volume, by source."""
    found = await ingestion.find_existing(
        source_pattern=f"{units.SOURCE_PREFIX}{volume_id}#"
    )
    by_source: dict[str, list[dict[str, Any]]] = {}
    for doc in found:
        by_source.setdefault(doc["source"], []).append(doc)
    return by_source


async def _events_for(event: Any, letter_id: str) -> list[Any]:
    found, _ = await event.query(
        EventFilter(
            event_types=[units.EVENT_TYPE], payload={"letter_document_id": letter_id}
        ),
        k=100,
    )
    return list(found)


async def _supersede(
    event: Any,
    ingestion: Any,
    existing: dict[str, list[dict[str, Any]]],
    kept: set[str],
) -> list[dict[str, Any]]:
    """Remove letters an earlier run made that this run no longer produces.

    A letter whose span moved is a new document under the same source; one
    whose opening vanished has no counterpart at all. Either way the old one
    would otherwise stay searchable beside its replacement. A document that
    something pins — a citation, a claim anchor — refuses deletion, and is
    reported rather than forced.
    """
    removed: list[dict[str, Any]] = []
    for docs in existing.values():
        for doc in docs:
            letter_id = str(doc["document_id"])
            if letter_id in kept:
                continue
            entry = {"letter_document_id": letter_id, "title": doc.get("title")}
            try:
                for old in await _events_for(event, letter_id):
                    await event.delete(old.id)
                entry["deleted"] = await ingestion.delete_document(letter_id)
            except Exception as exc:  # noqa: BLE001 - report the pin, keep going
                entry["deleted"] = False
                entry["why_not"] = f"{type(exc).__name__}: {exc}"
            removed.append(entry)
    return removed


def _reviewed_dates(
    found: list[openings.Opening],
    existing: dict[str, list[dict[str, Any]]],
    accept: list[dict[str, Any]],
) -> tuple[dict[int, Day], list[str]]:
    """Dates a reviewer settled: this run's `accept`, then earlier acceptances.

    An acceptance is kept on the letter document (``reviewer_date``), so a later
    run without `accept` does not quietly send it back to the queue.
    """
    by_start = {opening.start: index for index, opening in enumerate(found)}
    by_document: dict[str, int] = {}
    reviewed: dict[int, Day] = {}
    for docs in existing.values():
        for doc in docs:
            meta = doc.get("metadata") or {}
            index = by_start.get(meta.get("parent_char_start"))
            if index is None:
                continue
            by_document[str(doc["document_id"])] = index
            accepted = meta.get("review_status") == "accepted" and meta.get("reviewer_date")
            if accepted and (day := dating.from_resolved(meta["reviewer_date"])) is not None:
                reviewed[index] = day
    errors: list[str] = []
    for entry in accept:
        letter_id = str(entry.get("letter_document_id") or "")
        index = by_document.get(letter_id)
        day = dating.iso_day(entry.get("date"))
        if index is None:
            errors.append(f"accept: no letter of this volume is document {letter_id or '?'}.")
        elif day is None:
            errors.append(f"accept: {entry.get('date')!r} for {letter_id} is not a date.")
        else:
            reviewed[index] = day
    return reviewed, errors


def _neighbours(letters: list[units.Unit]) -> list[dict[str, Any]]:
    """For each letter, the nearest dated letters either side — review context."""
    dated = [(u.index, u.decision.day) for u in letters if u.decision.materialized]
    result = []
    for unit in letters:
        before = [(i, d) for i, d in dated if i < unit.index]
        after = [(i, d) for i, d in dated if i > unit.index]
        result.append(
            {
                "previous": _neighbour(letters, before[-1]) if before else None,
                "next": _neighbour(letters, after[0]) if after else None,
            }
        )
    return result


def _neighbour(letters: list[units.Unit], item: tuple[int, Day]) -> dict[str, Any]:
    index, day = item
    unit = letters[index]
    return {
        "unit_index": index + 1,
        "date": day.iso(),
        "dateline_as_written": unit.opening.get("dateline"),
        "place_reading": unit.opening.get("place"),
    }


def _report(
    *,
    volume_id: str,
    schema: str,
    dry_run: bool,
    tolerance: int | None,
    records: int,
    placement: openings.Placement,
    letters: list[units.Unit],
    existing: dict[str, list[dict[str, Any]]],
    notes: list[str],
) -> dict[str, Any]:
    held: dict[str, int] = {}
    readers = {"agree": 0, "disagree": 0, "single": 0}
    for unit in letters:
        if unit.decision.hold:
            held[unit.decision.hold] = held.get(unit.decision.hold, 0) + 1
        if unit.decision.readers in readers:
            readers[unit.decision.readers] += 1
    return {
        "volume_id": volume_id,
        "schema": schema,
        "dry_run": dry_run,
        "chronology_tolerance_days": tolerance,
        "summary": {
            "records_read": records,
            "duplicate_readings_merged": placement.merged,
            "openings_in_excluded_ranges": placement.excluded,
            "misplaced_records": len(placement.misplaced),
            "letters": len(letters),
            "materialized": sum(1 for u in letters if u.decision.materialized),
            "held": sum(held.values()),
            "held_by_reason": dict(sorted(held.items())),
            "readers": readers,
            "existing_letter_documents": sum(len(docs) for docs in existing.values()),
        },
        "letters": [_letter_row(unit) for unit in letters],
        "misplaced": placement.misplaced,
        "notes": notes,
    }


def _letter_row(unit: units.Unit) -> dict[str, Any]:
    decision = unit.decision
    return {
        "unit_index": unit.index + 1,
        "char_start": unit.start,
        "char_end": unit.end,
        "title": units.title_for(unit),
        "opening": unit.opening.get("opening"),
        "dateline": unit.opening.get("dateline"),
        "decision": "materialize" if decision.materialized else "hold",
        "hold_reason": decision.hold,
        "date": decision.day.iso() if decision.materialized and decision.day else None,
        "candidate_date": decision.candidate.iso() if decision.hold and decision.candidate else None,
        "date_source": decision.date_source,
        "readers": decision.readers,
        "confidence": round(decision.confidence, 3) if decision.materialized else None,
        "deviation_days": decision.deviation_days,
        "flags": sorted(set(decision.flags) | set(unit.flags)),
        "sender": unit.sender,
        "recipient": unit.recipient,
        "place": unit.opening.get("place"),
        "received": decision.received.iso() if decision.received else None,
    }


async def _review_queue(corpus: Any, volume_id: str | None) -> dict[str, Any]:
    wanted: dict[str, Any] = {"review_status": "needs_review"}
    if volume_id:
        wanted["parent_document_id"] = volume_id
    found = await corpus.find_documents(document_types=[LETTER_TYPE], metadata=wanted, limit=2000)
    held = []
    for doc in found:
        meta = doc.get("metadata") or {}
        held.append(
            {
                "letter_document_id": doc["id"],
                "title": doc.get("title"),
                "volume_document_id": meta.get("parent_document_id"),
                "unit_index": meta.get("unit_index"),
                "hold_reason": meta.get("hold_reason"),
                "dateline_as_written": meta.get("dateline_as_written"),
                "date_as_written": meta.get("date_as_written"),
                "candidate_date": meta.get("candidate_date"),
                "chronology_deviation_days": meta.get("chronology_deviation_days"),
                "neighbours": meta.get("neighbours"),
                "flags": meta.get("flags"),
            }
        )
    held.sort(key=lambda h: (str(h["volume_document_id"]), h["unit_index"] or 0))
    return {
        "held": held,
        "count": len(held),
        "how_to_accept": (
            "Re-run mode='structure', dry_run=false with accept=[{letter_document_id, "
            "date: 'YYYY-MM-DD' | 'YYYY-MM' | 'YYYY'}]. The acceptance is kept on the "
            "letter, so later runs do not send it back here."
        ),
    }


async def _configure(
    corpus: Any,
    ingestion: Any,
    volume_id: str,
    config: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    volume = await corpus.get_document(volume_id)
    if volume is None:
        return {"error": f"no document {volume_id}."}
    problems = [
        f"{key}: not a setting (settings: {', '.join(sorted(CONFIG_KEYS))})"
        for key in config
        if key not in CONFIG_KEYS
    ]
    problems += [
        f"{key}: expected {expected}"
        for key, expected in CONFIG_KEYS.items()
        if key in config and not isinstance(config[key], expected)
    ]
    for pair in config.get("excluded_ranges") or []:
        if not (
            isinstance(pair, list | tuple)
            and len(pair) == 2
            and all(isinstance(bound, int) for bound in pair)
            and pair[0] < pair[1]
        ):
            problems.append(f"excluded_ranges: {pair!r} is not [start, end] with start < end")
    if problems:
        return {"error": "invalid configuration", "problems": problems}
    change = {
        "volume_id": volume_id,
        "document_type": {"from": volume.get("document_type"), "to": COLLECTION_TYPE},
        "metadata": config,
        "dry_run": dry_run,
    }
    if dry_run:
        return change
    stored = await ingestion.update_document(
        volume_id, document_type=COLLECTION_TYPE, metadata=config
    )
    return {**change, "stored": stored}
