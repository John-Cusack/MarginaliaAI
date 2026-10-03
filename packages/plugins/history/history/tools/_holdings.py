"""What letters does this corpus actually hold?

`find_missing_letters` reported every reference it found as a candidate, which
made it a list of letters *mentioned*, not letters *absent* — the two are only
the same if you hold none of them.

Deciding absence needs three things, and the third is easy to skip:

1. What the reference says: who, and when.
2. What the corpus holds: its dated sections, one per letter.
3. **Which direction each holds.** An edition of a man's outgoing papers holds
   none of his incoming mail, so every "yours of the 30th" in it is trivially
   absent. Reporting hundreds of those as discoveries buries the handful that
   are real. Said once, as a property of the corpus, it is a useful caveat.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from history.tools._correspondence import A_TO_B, B_TO_A, between
from research_engine_sdk import EventFilter

#: Ranks, honorifics and offices that precede a name without being part of it.
_NOISE = {
    "genl", "gen", "general", "maj", "major", "lt", "lieut", "lieutenant",
    "col", "colonel", "capt", "captain", "brig", "brigadier", "hon",
    "honorable", "mr", "mrs", "dr", "his", "her", "excellency", "the", "secty",
    "secretary", "of", "war", "president", "esq", "comdg", "commanding", "us",
    "army", "sir", "my", "dear", "state", "treasury", "navy", "govr", "gov",
    "governor", "your", "presdt", "prest", "adj", "adjt", "adjutant", "to",
    "from", "asst", "assistant", "chief", "staff", "private", "confidential",
}
_PRONOUNS = {"you", "him", "her", "them", "me", "us", "he", "she", "it", "they"}

#: Which way a reference points, from the kind of reference it is.
SENT = "sent_by_author"
RECEIVED = "received_by_author"
UNKNOWN = "unknown"
_DIRECTION = {
    "prior_letter": SENT,
    "enclosure": SENT,
    "received_letter": RECEIVED,
    "mentioned_letter": UNKNOWN,
    "third_party_letter": UNKNOWN,
}


def surname(name: str | None) -> str | None:
    """The one part of a name every variant of it agrees on.

    "Stanton", "Edwin M. Stanton" and "Hon E M Stanton Secty of War" are one
    man; rank and office differ, the surname does not.
    """
    if not name:
        return None
    cleaned = re.sub(r"^\s*GBM\s+to\s+", "", name, flags=re.I)
    cleaned = re.sub(r"[^\w\s\.]", " ", cleaned).strip()
    words = [w for w in cleaned.split() if w.strip(".").lower() not in _NOISE]
    words = [w for w in words if not re.fullmatch(r"[A-Z]\.?", w.strip())]
    if not words:
        return None
    last = words[-1].strip(".").lower()
    if last in _PRONOUNS or len(last) < 3 or last.isdigit():
        return None
    return last


def direction_of(reference_type: str | None) -> str:
    return _DIRECTION.get(reference_type or "", UNKNOWN)


class Holdings:
    """The letters a corpus contains, indexed by correspondent and date."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], str] = {}
        self.outgoing = 0
        self.incoming = 0
        self.undirected = 0

    def add(self, title: str | None, date_start: str | None) -> None:
        if not title or not date_start:
            return
        stripped = title.strip()
        lowered = stripped.lower()
        if lowered.startswith("to "):
            self.outgoing += 1
        elif lowered.startswith("from "):
            self.incoming += 1
        else:
            self.undirected += 1
        who = surname(stripped)
        day = day_of(date_start)
        if who and day:
            self._by_key[(who, day)] = stripped

    def held(self, who: str | None, date_start: str | None) -> str | None:
        """The title of the letter matching this correspondent and day, if any."""
        day = day_of(date_start)
        if not who or not day:
            return None
        return self._by_key.get((who, day))

    @property
    def total(self) -> int:
        return len(self._by_key)

    def coverage_note(self) -> str | None:
        """Say once what the corpus cannot contain, rather than per reference."""
        if self.outgoing and not self.incoming:
            return (
                f"This corpus holds {self.outgoing} letters written by the "
                f"author and none received by him, so an inbound reference is "
                f"necessarily absent from it — that is a property of the "
                f"edition, not a discovery about the archive."
            )
        if self.incoming and not self.outgoing:
            return (
                f"This corpus holds {self.incoming} letters received by the "
                f"author and none sent by him, so an outbound reference is "
                f"necessarily absent from it."
            )
        return None


def day_of(value: str | None) -> str | None:
    """The calendar day of an ISO timestamp, parsed rather than sliced.

    ``value[:10]`` reads a day out of an ISO string only while every writer
    formats one the same way; parsing it does not depend on that.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError:
        return None


class LetterHoldings:
    """Letters held between two correspondents, by sender, recipient and day.

    Built from `letter_sent` events, whose actors are entity ids — not from
    titles, whose surnames fail on OCR ("FAEADAT"), on two people sharing one,
    and on direction. A reference is *held* by a letter of the same direction on
    the same day; a letter one day either side is a *near miss* — reported, never
    counted as held, because "yours of the 3d" and a letter dated the 4th may be
    two letters.
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, date], dict[str, Any]] = {}
        self.a_to_b = 0
        self.b_to_a = 0

    def add(self, sender: str, recipient: str, day: date, letter: dict[str, Any]) -> None:
        self._by_key.setdefault((sender, recipient, day), letter)

    def held(self, sender: str, recipient: str, day: date) -> dict[str, Any] | None:
        return self._by_key.get((sender, recipient, day))

    def near_miss(self, sender: str, recipient: str, day: date) -> dict[str, Any] | None:
        for offset in (-1, 1):
            if hit := self._by_key.get((sender, recipient, day + timedelta(days=offset))):
                return hit
        return None

    @property
    def total(self) -> int:
        return len(self._by_key)


async def from_events(event: Any, a: str, b: str) -> LetterHoldings:
    """Index the `letter_sent` events between *a* and *b* by direction and day."""
    holdings = LetterHoldings()
    if event is None:
        return holdings
    try:
        events, _ = await event.query(
            EventFilter(event_types=["letter_sent"], actor_entity_ids=[a, b]), k=10000
        )
    except Exception:  # noqa: BLE001 - no event store means no event holdings
        return holdings
    pair = await between(event, events, a, b)
    for letter, direction in pair.letters:
        if letter.timestamp_start is None or direction not in (A_TO_B, B_TO_A):
            continue
        sender, recipient = (a, b) if direction == A_TO_B else (b, a)
        payload = letter.payload or {}
        holdings.add(
            sender,
            recipient,
            letter.timestamp_start.date(),
            {
                "event_id": str(letter.id),
                "letter_document_id": payload.get("letter_document_id"),
                "date": letter.timestamp_start.date().isoformat(),
            },
        )
        if direction == A_TO_B:
            holdings.a_to_b += 1
        else:
            holdings.b_to_a += 1
    return holdings


async def build(corpus: Any, passage_ids: list[str]) -> Holdings:
    """Index the dated sections of every document these passages come from."""
    holdings = Holdings()
    documents: set[str] = set()
    for passage_id in passage_ids:
        try:
            context = await corpus.get_passage_context(passage_id)
        except Exception:  # noqa: BLE001 - a passage we cannot place is skipped
            continue
        if document_id := context.get("document_id"):
            documents.add(document_id)

    for document_id in documents:
        try:
            sections = await corpus.get_document_outline(document_id, dated_only=True)
        except (AttributeError, TypeError):
            # An older core, whose corpus client cannot read structure at all.
            return holdings
        for section in sections:
            holdings.add(section.get("title"), (section.get("metadata") or {}).get("date_start"))
    return holdings
