"""Find missing letters between two correspondents."""

from __future__ import annotations

from typing import Any


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
    - content_inference: Contextual clues suggesting missing letters
    - all: Combine all methods
    """
    candidates = []

    # Method 1: Referenced letters not in corpus
    if method in ("all", "referenced"):
        # Query extraction records for epistolary_references
        refs = await extraction.query_records(
            record_type="epistolary_reference",
            filters={"referenced_party_entity_id": correspondent_b_entity_id},
            k=500,
        )
        for ref in refs:
            data = ref.get("data", ref) if isinstance(ref, dict) else {}
            conf = data.get("confidence", 0)
            if conf >= min_confidence:
                candidates.append({
                    "method": "referenced",
                    "expected_sender_entity_id": correspondent_b_entity_id,
                    "expected_recipient_entity_id": correspondent_a_entity_id,
                    "expected_date": data.get("referenced_date"),
                    "confidence": conf,
                    "evidence": {
                        "passage_id": data.get("passage_id"),
                        "span_text": data.get("evidence", ""),
                    },
                    "content_hints": [data.get("content_hint", "")],
                })

    # Method 2: Cadence analysis
    if method in ("all", "cadence"):
        # Query events for letter_sent between the correspondents
        filters = {
            "event_types": ["letter_sent"],
            "actor_entity_ids": [correspondent_a_entity_id, correspondent_b_entity_id],
        }
        if date_range:
            filters["date_range_start"] = date_range.get("start")
            filters["date_range_end"] = date_range.get("end")

        events, _ = await event.query(filters, k=10000)
        # Simple gap detection: flag periods longer than 2x median interval
        if len(events) > 2:
            sorted_events = sorted(events, key=lambda e: e.timestamp_start or "")
            intervals = []
            for i in range(1, len(sorted_events)):
                if sorted_events[i].timestamp_start and sorted_events[i - 1].timestamp_start:
                    delta = (sorted_events[i].timestamp_start - sorted_events[i - 1].timestamp_start).days
                    intervals.append((delta, i))

            if intervals:
                median_interval = sorted(intervals)[len(intervals) // 2][0]
                threshold = max(median_interval * 2, 14)  # At least 2 weeks
                for delta, idx in intervals:
                    if delta > threshold:
                        candidates.append({
                            "method": "cadence",
                            "expected_date": {
                                "start": str(sorted_events[idx - 1].timestamp_start),
                                "end": str(sorted_events[idx].timestamp_start),
                            },
                            "confidence": min(0.8, (delta / threshold) * 0.5),
                            "gap_days": delta,
                            "median_interval_days": median_interval,
                        })

    # Deduplicate and sort
    candidates.sort(key=lambda c: c.get("confidence", 0), reverse=True)

    return {
        "candidates": candidates,
        "summary": {
            "total_candidates": len(candidates),
            "by_method": _count_by_method(candidates),
        },
    }


def _count_by_method(candidates: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in candidates:
        m = c.get("method", "unknown")
        counts[m] = counts.get(m, 0) + 1
    return counts
