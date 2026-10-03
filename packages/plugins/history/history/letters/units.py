"""Cutting a volume into letters, and describing each one.

A letter runs from its opening to the next letter's opening, cut short at a
chapter heading or an excluded range: an editor's chapter of narrative is not
part of the letter that precedes it. The cut is by offset into the volume's
canonical text, never by structure node, because nodes are not letters — one
node of the Faraday volume holds six, and one letter runs across three nodes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from history.letters.dating import Day, Decision, event_span, format_day
from history.letters.openings import Opening, normalized

#: Where every letter document this pass creates says it came from. With the
#: offset it is deterministic, so a re-run finds the letter it made last time:
#: same text, same source, and ingest deduplicates to the existing document.
SOURCE_PREFIX = "letter-collection:"

#: The event a letter produces, and the roles its actors may take. EventActor
#: roles are free text in core; this table is the contract, and the pass
#: refuses to write a role that is not in it.
EVENT_TYPE = "letter_sent"
ROLES = {EVENT_TYPE: frozenset({"sender", "recipient"})}

#: Chapter headings as they survive OCR: CHAPTER, CHAPTEE, CIIAPTEll, CHAPTEK.
CHAPTER = re.compile(r"\bC(?:H|II)APT", re.I)

SCHEMA = "letter_openings:1"


def source_for(volume_id: str, start: int) -> str:
    return f"{SOURCE_PREFIX}{volume_id}#{start}"


@dataclass
class Unit:
    """One letter: where it lies in the volume, and what was decided about it."""

    index: int
    opening: Opening
    start: int
    end: int
    decision: Decision
    sender: dict[str, Any] = field(default_factory=dict)
    recipient: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    def source_for_volume(self, volume_id: str) -> str:
        return source_for(volume_id, self.start)


def cut_points(
    outline: list[dict[str, Any]],
    excluded_ranges: list[tuple[int, int]],
    pattern: re.Pattern[str] = CHAPTER,
) -> list[int]:
    """Offsets a letter may not run past: chapter starts, excluded ranges."""
    cuts = {
        int(node["char_start"])
        for node in outline
        if node.get("title") and pattern.search(str(node["title"])) and node.get("depth", 1) > 0
    }
    cuts.update(start for start, _end in excluded_ranges)
    return sorted(cuts)


def spans(
    openings: list[Opening], text: str, cuts: list[int]
) -> list[tuple[int, int]]:
    """``(start, end)`` of each letter, in order; trailing whitespace trimmed."""
    result: list[tuple[int, int]] = []
    for index, opening in enumerate(openings):
        stop = openings[index + 1].start if index + 1 < len(openings) else len(text)
        stop = min([stop, *[cut for cut in cuts if cut > opening.start]])
        while stop > opening.start and text[stop - 1].isspace():
            stop -= 1
        result.append((opening.start, stop))
    return result


def _entity(value: Any) -> dict[str, Any] | None:
    """An alias-map or default-sender entry: an id string or {entity_id, name}."""
    if not value:
        return None
    if isinstance(value, str):
        return {"entity_id": value, "name": None}
    if isinstance(value, dict) and value.get("entity_id"):
        return {"entity_id": str(value["entity_id"]), "name": value.get("name")}
    return None


def resolve_actors(
    opening: Opening,
    *,
    alias_map: dict[str, Any] | None = None,
    default_sender: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Sender and recipient, each as ``{surface, entity_id, name, how}``.

    The order is fixed: core's entity resolution, then the volume's alias map
    (kinship words and abbreviations that mean one person in this book only —
    "his mother" is a different person in every book), then, for a heading
    that names no writer at all, the edition's default sender. A name that was
    written but would not resolve is never replaced by the default: it may be
    someone else.
    """
    aliases = {normalized(key).strip(" ."): value for key, value in (alias_map or {}).items()}
    flags: list[str] = []
    resolved = []
    for role in ("sender", "recipient"):
        surface = opening.get(role)
        found = opening.get(f"{role}_resolved") or {}
        actor = {"surface": surface, "entity_id": None, "name": None, "how": None}
        if found.get("entity_id"):
            actor.update(entity_id=str(found["entity_id"]), name=found.get("canonical_name"), how="resolved")
        elif surface and (mapped := _entity(aliases.get(normalized(surface).strip(" .")))):
            actor.update(entity_id=mapped["entity_id"], name=mapped["name"], how="alias_map")
        elif role == "sender" and not surface and (default := _entity(default_sender)):
            actor.update(entity_id=default["entity_id"], name=default["name"], how="default_sender")
            flags.append("sender_by_edition_convention")
        if actor["entity_id"] is None and "actor_unresolved" not in flags:
            flags.append("actor_unresolved")
        resolved.append(actor)
    return resolved[0], resolved[1], flags


def title_for(unit: Unit) -> str:
    """``Michael Faraday to his mother, Geneva, 1 July 1814``."""
    writer = unit.sender.get("name") or unit.sender.get("surface") or "Unknown writer"
    reader = unit.recipient.get("name") or unit.recipient.get("surface") or "unknown recipient"
    place = unit.opening.get("place")
    day = unit.decision.day if unit.decision.materialized else None
    parts = [f"{_tidy(writer)} to {_tidy(reader)}"]
    if place:
        parts.append(str(place))
    parts.append(format_day(day))
    return ", ".join(parts)


def _tidy(name: str) -> str:
    """OCR'd capitals ("FARADAY", "MRS. FARADAY") read as names."""
    return name.title() if name.isupper() else name


def letter_metadata(unit: Unit, *, volume_id: str, unit_count: int) -> dict[str, Any]:
    """What a letter document carries about itself and its origin.

    ``parent_document_id`` / ``parent_char_start`` / ``parent_char_end`` are the
    unit convention core's enricher reads to date records extracted from the
    volume (`postprocess.UNIT_PARENT`). ``author`` and ``recipient`` hold names,
    which is what search's author and recipient filters match. The old offset
    is traceability, never identity: identity is the document's id.
    """
    decision = unit.decision
    opening = unit.opening
    held = decision.hold is not None
    return {
        "parent_document_id": volume_id,
        "parent_char_start": unit.start,
        "parent_char_end": unit.end,
        "unit_index": unit.index + 1,
        "unit_count": unit_count,
        "quoted_opening": opening.get("opening"),
        "dateline_as_written": opening.get("dateline"),
        "date_as_written": opening.get("date_written"),
        "weekday_as_written": opening.get("weekday"),
        "place_reading": opening.get("place"),
        "place_qualifier": opening.get("place_qualifier"),
        "received_as_written": opening.get("received_date"),
        "received": decision.received.iso() if decision.received else None,
        "anchor_kind": opening.get("anchor_kind"),
        "date_source": decision.date_source,
        "year_inferred_from": decision.year_inferred_from,
        "sender_surface": unit.sender.get("surface"),
        "sender_entity_id": unit.sender.get("entity_id"),
        "recipient_surface": unit.recipient.get("surface"),
        "recipient_entity_id": unit.recipient.get("entity_id"),
        "author": unit.sender.get("name") or unit.sender.get("surface"),
        "recipient": unit.recipient.get("name") or unit.recipient.get("surface"),
        "source_record_ids": opening.record_ids,
        "schema": SCHEMA,
        "confidence": decision.confidence if not held else None,
        "flags": sorted(set(decision.flags) | set(unit.flags)),
        "review_status": _review_status(decision),
        "hold_reason": decision.hold,
        "candidate_date": decision.candidate.iso() if held and decision.candidate else None,
        "chronology_deviation_days": decision.deviation_days,
        "structured_by": "history.structure_letters",
    }


def _review_status(decision: Decision) -> str:
    if decision.hold is not None:
        return "needs_review"
    if "reviewed" in decision.flags:
        return "accepted"
    return "auto"


def document_dates(decision: Decision) -> dict[str, Any]:
    if not decision.materialized:
        return {}
    day: Day = decision.day  # type: ignore[assignment]
    start, end = event_span(day)
    return {
        "created_date_start": start,
        "created_date_end": end,
        "created_precision": day.precision,
    }


def event_for(
    unit: Unit,
    *,
    letter_document_id: str,
    source_passage_id: str,
    volume_id: str,
) -> dict[str, Any]:
    """The letter's `letter_sent` event. Direction lives in the actors."""
    decision = unit.decision
    day: Day = decision.day  # type: ignore[assignment]
    start, end = event_span(day)
    actors = [
        {"entity_id": actor["entity_id"], "role": role}
        for role, actor in (("sender", unit.sender), ("recipient", unit.recipient))
        if actor.get("entity_id")
    ]
    for actor in actors:
        if actor["role"] not in ROLES[EVENT_TYPE]:
            raise ValueError(f"role {actor['role']!r} is not a {EVENT_TYPE} role")
    return {
        "event_type": EVENT_TYPE,
        "timestamp_start": start,
        "timestamp_end": end,
        "precision": day.precision,
        "location_text": unit.opening.get("place"),
        "source_passage_id": source_passage_id,
        "confidence": decision.confidence,
        "actors": actors,
        "payload": {
            "letter_document_id": letter_document_id,
            "volume_document_id": volume_id,
            "schema": SCHEMA,
            "record_ids": unit.opening.record_ids,
            # A cached copy for display; direction is read from the actors.
            "sender_entity_id": unit.sender.get("entity_id"),
            "recipient_entity_id": unit.recipient.get("entity_id"),
            "sender_surface": unit.sender.get("surface"),
            "recipient_surface": unit.recipient.get("surface"),
            "date_as_written": unit.opening.get("date_written"),
            "weekday_as_written": unit.opening.get("weekday"),
            "dateline_as_written": unit.opening.get("dateline"),
            "place_reading": unit.opening.get("place"),
            "place_qualifier": unit.opening.get("place_qualifier"),
            "date_source": decision.date_source,
            "year_inferred_from": decision.year_inferred_from,
            "received": decision.received.iso() if decision.received else None,
            "flags": sorted(set(decision.flags) | set(unit.flags)),
        },
    }
