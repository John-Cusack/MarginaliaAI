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

    if method in ("all", "referenced"):
        candidates.extend(
            await _referenced(
                extraction,
                correspondent_a_entity_id,
                correspondent_b_entity_id,
                min_confidence,
                notes,
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
        "candidates": candidates,
        "notes": notes,
        "summary": {
            "total_candidates": len(candidates),
            "by_method": _count_by_method(candidates),
        },
    }


async def _referenced(
    extraction: Any,
    correspondent_a_entity_id: str,
    correspondent_b_entity_id: str,
    min_confidence: float,
    notes: list[str],
) -> list[dict[str, Any]]:
    """Letters named in letters we hold, that we do not hold."""
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

    candidates = []
    undated = 0
    for record in records:
        data = record.get("data", {}) if isinstance(record, dict) else {}
        confidence = data.get("confidence", 0)
        if confidence < min_confidence:
            continue
        expected_date = data.get(f"referenced_date{RESOLVED}")
        if expected_date is None:
            undated += 1
        candidates.append({
            "method": "referenced",
            "expected_sender_entity_id": correspondent_b_entity_id,
            "expected_recipient_entity_id": correspondent_a_entity_id,
            "expected_date": expected_date,
            # What the letter actually said, kept beside the parsed form so a
            # reader can judge the interpretation.
            "expected_date_as_written": data.get("referenced_date"),
            "confidence": confidence,
            "evidence": {
                # `passage_id` sits beside `data`, not inside it. Reading it
                # from `data` returned None for every candidate, so nothing
                # could be traced back to the letter that referenced it.
                "passage_id": record.get("passage_id") if isinstance(record, dict) else None,
                "span_text": data.get("evidence", ""),
            },
            "content_hints": [data.get("content_hint", "")],
        })

    if undated:
        notes.append(
            f"{undated} of {len(candidates)} referenced letters carry a date "
            f"phrase that could not be resolved. A relative date — \"the 3d "
            f"ult.\" — needs the date of the letter it appears in, and these "
            f"documents have none stored."
        )
    return candidates


async def _cadence(
    event: Any,
    correspondent_a_entity_id: str,
    correspondent_b_entity_id: str,
    date_range: dict | None,
    notes: list[str],
) -> list[dict[str, Any]]:
    """Stretches longer than this correspondence's own rhythm."""
    from research_engine.domain.events import EventFilter

    events, _ = await event.query(
        EventFilter(
            event_types=["letter_sent"],
            actor_entity_ids=[correspondent_a_entity_id, correspondent_b_entity_id],
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
