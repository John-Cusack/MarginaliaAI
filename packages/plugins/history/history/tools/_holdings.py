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
from typing import Any

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
        if who:
            self._by_key[(who, date_start[:10])] = stripped

    def held(self, who: str | None, date_start: str | None) -> str | None:
        """The title of the letter matching this correspondent and day, if any."""
        if not who or not date_start:
            return None
        return self._by_key.get((who, date_start[:10]))

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
