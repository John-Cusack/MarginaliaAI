"""Which letters passed between two correspondents, and in which direction.

Both history tools used to ask the event store for `letter_sent` events whose
actors include *either* correspondent — an OR — and then read direction from a
``payload.sender_entity_id`` that nothing wrote. So a Faraday–Abbott cadence
counted every letter Faraday wrote to his mother, and every letter whose sender
it could not read landed in ``b_to_a`` by default.

Direction is an actor's role (``sender`` / ``recipient``), stored in
``event_actors``. A letter is between A and B only when one sends and the other
receives; one whose other party is unresolved is not counted as theirs, and a
letter naming both without saying who sent it is ``unknown``, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

A_TO_B = "a_to_b"
B_TO_A = "b_to_a"
UNKNOWN = "unknown"


@dataclass
class PairLetters:
    """The pair's letters with their direction, and what was left out."""

    letters: list[tuple[Any, str]]
    seen: int
    other_parties: int
    unattributed: int
    actors_from: str  # "event_actors" | "payload" (an older core)

    @property
    def counts(self) -> dict[str, int]:
        found = {A_TO_B: 0, B_TO_A: 0, UNKNOWN: 0}
        for _event, direction in self.letters:
            found[direction] += 1
        return found


async def between(event_client: Any, events: list[Any], a: str, b: str) -> PairLetters:
    """Keep the events that are letters between *a* and *b*, with direction."""
    actors, source = await _actors(event_client, events)
    letters: list[tuple[Any, str]] = []
    other_parties = unattributed = 0
    for event in events:
        roles = actors.get(str(event.id))
        if roles is None:
            payload = event.payload or {}
            roles = {
                "sender": _id(payload.get("sender_entity_id")),
                "recipient": _id(payload.get("recipient_entity_id")),
                "people": {
                    p
                    for p in (
                        _id(payload.get("sender_entity_id")),
                        _id(payload.get("recipient_entity_id")),
                    )
                    if p
                },
            }
        sender, recipient, people = roles["sender"], roles["recipient"], roles["people"]
        if sender == a and recipient == b:
            letters.append((event, A_TO_B))
        elif sender == b and recipient == a:
            letters.append((event, B_TO_A))
        elif {a, b} <= people:
            letters.append((event, UNKNOWN))
        elif people & {a, b} and (sender is None or recipient is None):
            unattributed += 1
        else:
            other_parties += 1
    return PairLetters(letters, len(events), other_parties, unattributed, source)


async def _actors(
    event_client: Any, events: list[Any]
) -> tuple[dict[str, dict[str, Any]], str]:
    getter = getattr(event_client, "get_actors_many", None)
    if getter is None or not events:
        return {}, "payload"
    raw = await getter([str(event.id) for event in events])
    result: dict[str, dict[str, Any]] = {}
    for event_id, actors in raw.items():
        roles: dict[str, Any] = {"sender": None, "recipient": None, "people": set()}
        for actor in actors:
            entity = _id(actor.get("entity_id"))
            if not entity:
                continue
            roles["people"].add(entity)
            if actor.get("role") in ("sender", "recipient") and roles[actor["role"]] is None:
                roles[actor["role"]] = entity
        result[str(event_id)] = roles
    return result, "event_actors"


def _id(value: Any) -> str | None:
    return str(value) if value else None
