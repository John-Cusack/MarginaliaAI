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

from datetime import datetime
from typing import Any
from uuid import UUID

from history.tools import _holdings

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
    verdicts: dict[str, list] = {"missing": [], "held": [], "undetermined": []}

    if method in ("all", "referenced"):
        candidates.extend(
            await _referenced(
                corpus,
                extraction,
                correspondent_a_entity_id,
                correspondent_b_entity_id,
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
        # Referenced, but not checkable. Kept visible so the gap between
        # "not found" and "not looked for" stays legible.
        "undetermined": verdicts["undetermined"],
        "notes": notes,
        "summary": {
            "total_candidates": len(candidates),
            "referenced_and_held": len(verdicts["held"]),
            "not_checkable": len(verdicts["undetermined"]),
            "by_method": _count_by_method(candidates),
        },
    }


async def _referenced(
    corpus: Any,
    extraction: Any,
    correspondent_a_entity_id: str,
    correspondent_b_entity_id: str,
    min_confidence: float,
    notes: list[str],
    verdicts: dict[str, list],
) -> list[dict[str, Any]]:
    """Letters named in letters we hold, that we do not hold.

    The second half of that sentence is new. This used to return every
    reference above the confidence threshold, which is a list of letters
    *mentioned* — the same thing as letters *missing* only if you hold none of
    them. Each reference is now looked up against the corpus's own dated
    sections and sorted into held, missing, or undetermined.

    Undetermined is not a failure to be tidied away. A reference whose date
    would not resolve cannot be looked up at all, and calling it missing would
    manufacture a gap out of a parsing limitation.
    """
    records = await extraction.query_records(
        record_type="epistolary_reference",
        filters={
            f"referenced_party_entity_id{RESOLVED}": {
                "entity_id": correspondent_b_entity_id
            }
        },
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
    holdings = await _holdings.build(
        corpus, [record.get("passage_id") for record in considered if record.get("passage_id")]
    )
    if holdings.total == 0:
        notes.append(
            "The corpus reports no dated sections for these documents, so no "
            "reference could be checked against what is held. Run "
            "`research-engine reindex structure` to date them."
        )
    elif (coverage := holdings.coverage_note()) is not None:
        notes.append(coverage)

    missing: list[dict[str, Any]] = []
    for record in considered:
        data = record.get("data") or {}
        resolved = data.get(f"referenced_date{RESOLVED}") or {}
        when = resolved.get("start")
        who = _holdings.surname(data.get("referenced_party_surface"))
        entry = {
            "method": "referenced",
            "expected_sender_entity_id": correspondent_b_entity_id,
            "expected_recipient_entity_id": correspondent_a_entity_id,
            "expected_date": resolved or None,
            "expected_date_as_written": data.get("referenced_date"),
            "direction": _holdings.direction_of(data.get("reference_type")),
            "confidence": data.get("confidence", 0),
            "evidence": {
                "passage_id": record.get("passage_id"),
                "span_text": data.get("evidence", ""),
            },
            "content_hints": [data.get("content_hint", "")],
        }

        if not when:
            entry["undetermined_because"] = (
                "the date it gives could not be resolved, so there is nothing "
                "to look up"
            )
            verdicts["undetermined"].append(entry)
            continue

        if (title := holdings.held(who, when)) is not None:
            entry["held_as"] = title
            verdicts["held"].append(entry)
            continue

        entry["absent_from"] = f"{holdings.total} dated letters in this corpus"
        missing.append(entry)

    verdicts["missing"].extend(missing)
    if verdicts["undetermined"]:
        notes.append(
            f"{len(verdicts['undetermined'])} of {len(considered)} referenced "
            f"letters could not be checked: their date would not resolve, and a "
            f"reference with no date cannot be looked up. They are reported "
            f"separately rather than counted as missing."
        )
    return missing


async def _cadence(
    event: Any,
    correspondent_a_entity_id: str,
    correspondent_b_entity_id: str,
    date_range: dict | None,
    notes: list[str],
) -> list[dict[str, Any]]:
    """Stretches longer than this correspondence's own rhythm."""
    from research_engine.domain.events import EventFilter

    # MCP hands these over as strings; `EventFilter.actor_entity_ids` is typed
    # `list[UUID]` and rejects anything else outright, so the whole tool raised
    # before it looked at a single event.
    actors = [_as_uuid(correspondent_a_entity_id), _as_uuid(correspondent_b_entity_id)]
    actors = [a for a in actors if a is not None]
    if not actors:
        notes.append(
            "Cadence analysis needs at least one correspondent identified by "
            "entity id; neither argument was a usable one."
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

    dated = sorted(
        (e for e in events if e.timestamp_start is not None),
        key=lambda e: e.timestamp_start,
    )
    if len(dated) < 3:
        notes.append(
            f"Cadence analysis needs at least three dated letters between the "
            f"two correspondents; found {len(dated)} among {len(events)} "
            f"letter_sent events. Without dates there is no rhythm to find a "
            f"gap in, so this method reports nothing rather than no gaps."
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
