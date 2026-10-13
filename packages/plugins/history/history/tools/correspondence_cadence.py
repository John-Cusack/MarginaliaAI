"""Correspondence cadence between two people.

Counts only the letters that passed between them — one sends, the other
receives — and reads direction from the letters' actors. See `_correspondence`
for why both halves of that sentence used to be wrong.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from history.tools._correspondence import A_TO_B, B_TO_A, UNKNOWN, between
from research_engine_sdk import EventFilter


async def tool_handler(
    event: Any,
    correspondent_a_entity_id: str,
    correspondent_b_entity_id: str,
    date_range: dict | None = None,
    time_bin: str = "month",
) -> dict[str, Any]:
    """Analyze correspondence cadence between two entities.

    Returns density timeline and flagged anomalies.
    """
    a, b = str(correspondent_a_entity_id), str(correspondent_b_entity_id)
    filters = {
        "event_types": ["letter_sent"],
        "actor_entity_ids": [UUID(a), UUID(b)],
    }
    if date_range:
        filters["date_range_start"] = date_range.get("start")
        filters["date_range_end"] = date_range.get("end")

    events, _ = await event.query(EventFilter.model_validate(filters), k=10000)
    pair = await between(event, events, a, b)

    bins: dict[str, dict[str, int]] = defaultdict(lambda: {A_TO_B: 0, B_TO_A: 0, UNKNOWN: 0})
    undated = 0
    for evt, direction in pair.letters:
        ts = evt.timestamp_start
        if not ts:
            undated += 1
            continue
        if time_bin == "month":
            key = ts.strftime("%Y-%m")
        elif time_bin == "week":
            key = ts.strftime("%Y-W%W")
        else:
            key = ts.strftime("%Y-%m-%d")
        bins[key][direction] += 1

    all_bins = sorted(bins)
    totals = [sum(bins[key].values()) for key in all_bins]
    avg = sum(totals) / len(totals) if totals else 0
    anomalies = []
    for key, count in zip(all_bins, totals, strict=True):
        if count == 0 and avg > 0:
            anomalies.append({"bin": key, "type": "silence", "expected": round(avg, 1)})
        elif count > avg * 3 and avg > 0:
            anomalies.append(
                {"bin": key, "type": "burst", "count": count, "expected": round(avg, 1)}
            )

    counts = pair.counts
    notes = []
    if pair.other_parties:
        notes.append(
            f"{pair.other_parties} letters name one of the two with someone else, "
            f"and are not counted."
        )
    if pair.unattributed:
        notes.append(
            f"{pair.unattributed} letters name one of the two and an unresolved "
            f"other party; they may belong here and are not counted."
        )
    if counts[UNKNOWN]:
        notes.append(
            f"{counts[UNKNOWN]} letters name both without saying who sent which; "
            f"they are counted under 'unknown', never assigned a direction."
        )
    if pair.actors_from == "payload":
        notes.append("Direction was read from event payloads: this core has no get_actors_many.")
    return {
        "timeline": [
            {
                "bin": key,
                "a_to_b": bins[key][A_TO_B],
                "b_to_a": bins[key][B_TO_A],
                "unknown": bins[key][UNKNOWN],
                "total": sum(bins[key].values()),
            }
            for key in all_bins
        ],
        "summary": {
            "total_letters": len(pair.letters),
            "a_to_b_count": counts[A_TO_B],
            "b_to_a_count": counts[B_TO_A],
            "unknown_direction_count": counts[UNKNOWN],
            "undated_letters": undated,
            "events_examined": pair.seen,
            "time_span_bins": len(all_bins),
            "average_per_bin": round(avg, 1),
        },
        "anomalies": anomalies,
        "notes": notes,
    }
