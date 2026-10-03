"""Find missing letters between two correspondents.

Both detection methods rested on dates the engine was not producing.

The *referenced* method reads ``epistolary_reference`` records — "yours of the
3d ult." — and filtered them on ``referenced_party_entity_id``, a field the
model was asked to fill with a corpus UUID it could not possibly know. Core now
resolves ``entity_ref`` and ``fuzzy_date`` fields after extraction and writes the
structured form to ``<field>_resolved``, which is what this reads.

The *cadence* method compares intervals between dated events. It passed a plain
dict where the event service takes an ``EventFilter``, and when it did return
events it sorted them by a timestamp that is null for every event in this
corpus — producing no intervals, no candidates, and a report of "no missing
letters" indistinguishable from a real one.

So both methods now say what they could not do. A gap you can see is worth more
than a clean answer you cannot trust.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from history.tools import _holdings
from history.tools._correspondence import between
from research_engine_sdk import EventFilter

#: Suffix core appends when it resolves a declared field type into structured
#: form. Kept as a literal rather than imported: this is a pack, and reaching
#: into core's service internals is how packs break on an engine upgrade.
RESOLVED = "_resolved"


async def tool_handler(
    corpus: Any,
    extraction: Any,
    entity: Any,
    event: Any,
    correspondent_a_entity_id: str,
    correspondent_b_entity_id: str,
    date_range: dict | None = None,
    method: str = "all",
    min_confidence: float = 0.6,
) -> dict[str, Any]:
    """Detect likely missing letters between two correspondents.

    Methods:
    - referenced: Letters explicitly referenced in existing correspondence
    - cadence: Gaps in expected correspondence rhythm
    - all: Combine all methods
    """
    candidates: list[dict[str, Any]] = []
    notes: list[str] = []
    verdicts: dict[str, list] = {"missing": [], "held": [], "near_miss": [], "undetermined": []}

    if method in ("all", "referenced"):
        candidates.extend(
            await _referenced(
                corpus,
                extraction,
                event,
                str(correspondent_a_entity_id),
                str(correspondent_b_entity_id),
                min_confidence,
                notes,
                verdicts,
            )
        )

    if method in ("all", "cadence"):
        candidates.extend(
            await _cadence(
                event,
                correspondent_a_entity_id,
                correspondent_b_entity_id,
                date_range,
                notes,
            )
        )

    candidates.sort(key=lambda c: c.get("confidence", 0), reverse=True)
    return {
        # Checked against the corpus and absent from it.
        "candidates": candidates,
        # Referenced and found — evidence the check works, and the reason the
        # candidate list is shorter than the reference count.
        "held": verdicts["held"],
        # A held letter one day off: possibly the referenced one, possibly not.
        # Never counted as held, never as missing.
        "near_miss": verdicts["near_miss"],
        # Referenced, but not checkable. Kept visible so the gap between
        # "not found" and "not looked for" stays legible.
        "undetermined": verdicts["undetermined"],
        "notes": notes,
        "summary": {
            "total_candidates": len(candidates),
            "referenced_and_held": len(verdicts["held"]),
            "near_misses": len(verdicts["near_miss"]),
            "not_checkable": len(verdicts["undetermined"]),
            "by_method": _count_by_method(candidates),
        },
    }


async def _referenced(
    corpus: Any,
    extraction: Any,
    event: Any,
    correspondent_a_entity_id: str,
    correspondent_b_entity_id: str,
    min_confidence: float,
    notes: list[str],
    verdicts: dict[str, list],
) -> list[dict[str, Any]]:
    """Letters named in letters we hold, that we do not hold.

    Each reference is looked up against the letters the corpus holds between
    the two correspondents and sorted into held, near miss, missing, or
    undetermined. Holdings come from `letter_sent` events — sender, recipient
    and day, as entity ids — when there are any; otherwise from dated section
    titles, the older and weaker index.

    Direction comes from the reference's kind and from who wrote the letter that
    quotes it: "yours of the 3d" in a letter by A is a letter from B to A. When
    the quoting letter is a letter document with a resolved sender, that is the
    writer; otherwise correspondent A is assumed to be.

    Undetermined is not a failure to be tidied away. A reference whose date
    would not resolve cannot be looked up at all, and calling it missing would
    manufacture a gap out of a parsing limitation.
    """
    a, b = correspondent_a_entity_id, correspondent_b_entity_id
    records = await extraction.query_records(
        record_type="epistolary_reference",
        filters={f"referenced_party_entity_id{RESOLVED}": {"entity_id": b}},
        k=500,
    )
    if not records:
        notes.append(
            "No epistolary_reference records name that correspondent. Either the "
            "schema has not been run over this correspondence, or extraction "
            "could not resolve the name to an entity."
        )
        return []

    considered = [
        record
        for record in records
        if (record.get("data") or {}).get("confidence", 0) >= min_confidence
    ]
    letters = await _holdings.from_events(event, a, b)
    titles = None
    if letters.total:
        notes.append(
            f"Checked against {letters.total} dated letters between the two "
            f"({letters.a_to_b} one way, {letters.b_to_a} the other)."
        )
    else:
        titles = await _holdings.build(
            corpus, [record.get("passage_id") for record in considered if record.get("passage_id")]
        )
        if titles.total == 0:
            notes.append(
                "The corpus holds no dated letters between these two: no "
                "letter_sent events and no dated sections. Split their volume "
                "with history.structure_letters to date them."
            )
        elif (coverage := titles.coverage_note()) is not None:
            notes.append(coverage)

    writers: dict[str, str | None] = {}
    missing: list[dict[str, Any]] = []
    for record in considered:
        data = record.get("data") or {}
        resolved = data.get(f"referenced_date{RESOLVED}") or {}
        when = resolved.get("start")
        direction = _holdings.direction_of(data.get("reference_type"))
        writer = await _writer_of(corpus, record.get("passage_id"), writers) or a
        other = b if writer == a else a
        if direction == _holdings.SENT:
            pairs = [(writer, other)]
        elif direction == _holdings.RECEIVED:
            pairs = [(other, writer)]
        else:
            pairs = [(writer, other), (other, writer)]
        entry = {
            "method": "referenced",
            "expected_sender_entity_id": pairs[0][0],
            "expected_recipient_entity_id": pairs[0][1],
            "expected_date": resolved or None,
            "expected_date_as_written": data.get("referenced_date"),
            "direction": direction,
            "confidence": data.get("confidence", 0),
            "evidence": {
                "passage_id": record.get("passage_id"),
                "span_text": data.get("evidence", ""),
            },
            "content_hints": [data.get("content_hint", "")],
        }

        if writer not in (a, b):
            entry["undetermined_because"] = (
                "the letter quoting it is by neither correspondent"
            )
            verdicts["undetermined"].append(entry)
            continue
        day = _holdings.day_of(when)
        if not day:
            entry["undetermined_because"] = (
                "the date it gives could not be resolved, so there is nothing "
                "to look up"
            )
            verdicts["undetermined"].append(entry)
            continue

        if titles is None:
            on = date.fromisoformat(day)
            if hit := _first(letters.held, pairs, on):
                entry["held_as"] = hit
                verdicts["held"].append(entry)
                continue
            if near := _first(letters.near_miss, pairs, on):
                entry["near_miss_of"] = near
                verdicts["near_miss"].append(entry)
                continue
            entry["absent_from"] = f"{letters.total} dated letters between the two"
        else:
            who = _holdings.surname(data.get("referenced_party_surface"))
            if (title := titles.held(who, when)) is not None:
                entry["held_as"] = title
                verdicts["held"].append(entry)
                continue
            entry["absent_from"] = f"{titles.total} dated sections in this corpus"
        missing.append(entry)

    verdicts["missing"].extend(missing)
    if verdicts["undetermined"]:
        notes.append(
            f"{len(verdicts['undetermined'])} of {len(considered)} referenced "
            f"letters could not be checked: their date would not resolve, or the "
            f"letter quoting them is by someone else. They are reported "
            f"separately rather than counted as missing."
        )
    return missing


def _first(lookup: Any, pairs: list[tuple[str, str]], on: date) -> Any:
    """The first (sender, recipient) pair *lookup* finds a letter for."""
    for sender, recipient in pairs:
        if (hit := lookup(sender, recipient, on)) is not None:
            return hit
    return None


async def _writer_of(
    corpus: Any, passage_id: str | None, cache: dict[str, str | None]
) -> str | None:
    """The resolved sender of the letter document a passage belongs to."""
    if not passage_id:
        return None
    try:
        context = await corpus.get_passage_context(passage_id)
        document_id = context.get("document_id")
        if document_id not in cache:
            document = await corpus.get_document(document_id) if document_id else None
            meta = (document or {}).get("metadata") or {}
            cache[document_id] = meta.get("sender_entity_id")
        return cache.get(document_id)
    except Exception:  # noqa: BLE001 - an unplaceable passage has no known writer
        return None


async def _cadence(
    event: Any,
    correspondent_a_entity_id: str,
    correspondent_b_entity_id: str,
    date_range: dict | None,
    notes: list[str],
) -> list[dict[str, Any]]:
    """Stretches longer than this correspondence's own rhythm.

    Only letters between the two count: the actor filter matches either
    correspondent, and a man's letters to his mother say nothing about the
    rhythm of his letters to a friend.
    """

    # MCP hands these over as strings; `EventFilter.actor_entity_ids` is typed
    # `list[UUID]` and rejects anything else outright, so the whole tool raised
    # before it looked at a single event.
    actors = [_as_uuid(correspondent_a_entity_id), _as_uuid(correspondent_b_entity_id)]
    actors = [a for a in actors if a is not None]
    if len(actors) < 2:
        notes.append(
            "Cadence analysis needs both correspondents identified by entity id; "
            "at least one argument was not a usable one."
        )
        return []

    events, _ = await event.query(
        EventFilter(
            event_types=["letter_sent"],
            actor_entity_ids=actors,
            date_range_start=_as_datetime((date_range or {}).get("start")),
            date_range_end=_as_datetime((date_range or {}).get("end")),
        ),
        k=10000,
    )
    pair = await between(event, events, str(actors[0]), str(actors[1]))

    dated = sorted(
        (e for e, _direction in pair.letters if e.timestamp_start is not None),
        key=lambda e: e.timestamp_start,
    )
    if len(dated) < 3:
        notes.append(
            f"Cadence analysis needs at least three dated letters between the "
            f"two correspondents; found {len(dated)} among {len(events)} "
            f"letter_sent events naming either. Without dates there is no rhythm "
            f"to find a gap in, so this method reports nothing rather than no gaps."
        )
        return []

    intervals = [
        ((dated[i].timestamp_start - dated[i - 1].timestamp_start).days, i)
        for i in range(1, len(dated))
    ]
    median_interval = sorted(intervals)[len(intervals) // 2][0]
    threshold = max(median_interval * 2, 14)  # At least two weeks.

    return [
        {
            "method": "cadence",
            "expected_date": {
                "start": dated[index - 1].timestamp_start.isoformat(),
                "end": dated[index].timestamp_start.isoformat(),
            },
            "confidence": min(0.8, (delta / threshold) * 0.5),
            "gap_days": delta,
            "median_interval_days": median_interval,
        }
        for delta, index in intervals
        if delta > threshold
    ]


def _as_uuid(value: object) -> UUID | None:
    """An entity id, or None when the caller passed something that is not one."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _as_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _count_by_method(candidates: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in candidates:
        m = c.get("method", "unknown")
        counts[m] = counts.get(m, 0) + 1
    return counts
