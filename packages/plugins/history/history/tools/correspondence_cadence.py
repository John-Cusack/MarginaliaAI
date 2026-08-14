"""Correspondence cadence analysis tool."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


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
    filters = {
        "event_types": ["letter_sent"],
        "actor_entity_ids": [correspondent_a_entity_id, correspondent_b_entity_id],
    }
    if date_range:
        filters["date_range_start"] = date_range.get("start")
        filters["date_range_end"] = date_range.get("end")

    events, buckets = await event.query(filters, k=10000, group_by=time_bin)

    # Separate by direction
    a_to_b: dict[str, int] = defaultdict(int)
    b_to_a: dict[str, int] = defaultdict(int)

    for evt in events:
        ts = evt.timestamp_start
        if not ts:
            continue

        if time_bin == "month":
            bucket_key = ts.strftime("%Y-%m")
        elif time_bin == "week":
            bucket_key = ts.strftime("%Y-W%W")
        else:
            bucket_key = ts.strftime("%Y-%m-%d")

        # Determine direction from event actors/payload
        sender = evt.payload.get("sender_entity_id", "")
        if str(sender) == str(correspondent_a_entity_id):
            a_to_b[bucket_key] += 1
        else:
            b_to_a[bucket_key] += 1

    # All time bins
    all_bins = sorted(set(list(a_to_b.keys()) + list(b_to_a.keys())))

    # Detect anomalies (bins with significantly different counts)
    total_per_bin = [a_to_b.get(b, 0) + b_to_a.get(b, 0) for b in all_bins]
    avg = sum(total_per_bin) / len(total_per_bin) if total_per_bin else 0
    anomalies = []
    for i, b in enumerate(all_bins):
        count = total_per_bin[i]
        if count == 0 and avg > 0:
            anomalies.append({"bin": b, "type": "silence", "expected": round(avg, 1)})
        elif count > avg * 3 and avg > 0:
            anomalies.append({"bin": b, "type": "burst", "count": count, "expected": round(avg, 1)})

    return {
        "timeline": [
            {
                "bin": b,
                "a_to_b": a_to_b.get(b, 0),
                "b_to_a": b_to_a.get(b, 0),
                "total": a_to_b.get(b, 0) + b_to_a.get(b, 0),
            }
            for b in all_bins
        ],
        "summary": {
            "total_letters": len(events),
            "a_to_b_count": sum(a_to_b.values()),
            "b_to_a_count": sum(b_to_a.values()),
            "time_span_bins": len(all_bins),
            "average_per_bin": round(avg, 1),
        },
        "anomalies": anomalies,
    }
