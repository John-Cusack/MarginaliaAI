"""Deciding each letter's date — or why it is held for review.

The model reads each dateline (``date_written``); core reads the same verbatim
dateline with its scanner (``dateline_dates``) and parses the model's reading
(``date_written_resolved``). Those are two independent readers of one line of
print, and this module decides between them under rules a reviewer can predict:

- A printed year is taken when both readers agree, or when only the model
  could read the line (a French or day-first dateline the scanner cannot) —
  the second capped and flagged.
- A year the model gives that is not printed in the dateline is a hallucinated
  year, and held. Running heads and context never supply a year here.
- A yearless dateline takes the next occurrence after the previous dated
  letter, and only when the next dated letter brackets it.
- A date outside the order its neighbours set is held, never corrected: some
  letters are printed out of order on purpose (an 1836 letter placed in the
  1823 story), and some years are misprinted or misread (1816 for 1815).

A held letter keeps no date and produces no event; it waits in the review
queue with the reason, its neighbours and the line as printed.
"""

from __future__ import annotations

import calendar
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Any

#: The most a materialized date may claim, whatever the record says.
CAP_DEFAULT = 0.9
CAP_EDITORIAL = 0.8
CAP_SINGLE_READER = 0.75
CAP_INFERRED_YEAR = 0.75
CAP_WEEKDAY_MISMATCH = 0.6
CAP_REVIEWED = 0.9

#: How many dated letters either side set the local chronology.
NEIGHBOURS = 3

#: Hold reasons. A closed set, so the review queue can be filtered by them.
UNDATED = "undated"
CONJECTURAL = "conjectural"
UNREADABLE = "unreadable"
HALLUCINATED_YEAR = "hallucinated_year"
READER_DISAGREEMENT = "reader_disagreement"
CHRONOLOGY_CONFLICT = "chronology_conflict"
YEAR_UNBRACKETED = "year_unbracketed"
WEEKDAY_CONFLICT = "weekday_conflict"
SINGLE_READER_STRICT = "single_reader_strict"
HOLD_REASONS = frozenset(
    {
        UNDATED,
        CONJECTURAL,
        UNREADABLE,
        HALLUCINATED_YEAR,
        READER_DISAGREEMENT,
        CHRONOLOGY_CONFLICT,
        YEAR_UNBRACKETED,
        WEEKDAY_CONFLICT,
        SINGLE_READER_STRICT,
    }
)

MONTHS = {
    name.lower(): number
    for number, names in enumerate(
        [
            (), ("january", "jan", "jany"), ("february", "feb", "feby"),
            ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
            ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"),
            ("october", "oct"), ("november", "nov"), ("december", "dec"),
        ]
    )
    for name in names
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_YEAR = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")
_MONTH_DAY = re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th|d)?\b", re.I)
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th|d)?\s+({_MONTH})\b\.?", re.I)
_MONTH_ONLY = re.compile(rf"\b({_MONTH})\b\.?", re.I)
_CONJECTURE = re.compile(
    r"\bor\b|\?|\babout\b|\bcirca\b|\bc\.\s|\bmust have been\b|\bprobably\b|\bbetween\b",
    re.I,
)


@dataclass(frozen=True)
class Day:
    """A date and how much of it is known."""

    start: date
    end: date
    precision: str  # day | month | year

    def iso(self) -> dict[str, str]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "precision": self.precision,
        }


def year_in(text: str | None) -> int | None:
    match = _YEAR.search(text or "")
    return int(match.group(1)) if match else None


def month_day(text: str | None) -> tuple[int, int] | None:
    """The month and day a written date names, ignoring any year."""
    without_year = _YEAR.sub(" ", text or "")
    if match := _MONTH_DAY.search(without_year):
        return MONTHS[match.group(1).lower()], int(match.group(2))
    if match := _DAY_MONTH.search(without_year):
        return MONTHS[match.group(2).lower()], int(match.group(1))
    return None


def read_day(text: str | None) -> Day | None:
    """A written date with its year: day precision, or month when no day."""
    year = year_in(text)
    if year is None:
        return None
    if md := month_day(text):
        return _day(year, *md)
    if match := _MONTH_ONLY.search(_YEAR.sub(" ", text or "")):
        month = MONTHS[match.group(1).lower()]
        last = calendar.monthrange(year, month)[1]
        return Day(date(year, month, 1), date(year, month, last), "month")
    return Day(date(year, 1, 1), date(year, 12, 31), "year")


def iso_day(raw: str | None) -> Day | None:
    """A reviewer's date: ``1815-02-13``, ``1815-02`` or ``1815``."""
    parts = str(raw or "").strip().split("-")
    try:
        numbers = [int(part) for part in parts if part]
    except ValueError:
        return None
    if len(numbers) == 3:
        return _day(*numbers)
    if len(numbers) == 2 and 1 <= numbers[1] <= 12:
        year, month = numbers
        return Day(date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]), "month")
    if len(numbers) == 1 and len(parts) == 1:
        return Day(date(numbers[0], 1, 1), date(numbers[0], 12, 31), "year")
    return None


def from_resolved(resolved: dict[str, Any] | None) -> Day | None:
    """Core's `<field>_resolved` / `_dates` entry as a Day."""
    if not resolved or not resolved.get("start"):
        return None
    start = date.fromisoformat(str(resolved["start"])[:10])
    end = date.fromisoformat(str(resolved.get("end") or resolved["start"])[:10])
    precision = str(resolved.get("precision") or "day").rsplit(".", 1)[-1]
    return Day(start, end, precision)


def forward_from(anchor: date, month: int, day: int) -> Day | None:
    """The first (month, day) on or after *anchor*."""
    year = anchor.year if (month, day) >= (anchor.month, anchor.day) else anchor.year + 1
    return _day(year, month, day)


def _day(year: int, month: int, day: int) -> Day | None:
    try:
        when = date(year, month, day)
    except ValueError:
        return None
    return Day(when, when, "day")


def weekday_of(when: date) -> str:
    return WEEKDAYS[when.weekday()]


@dataclass
class Reading:
    """What dating needs from one opening, already pulled out of the record."""

    anchor_kind: str
    date_written: str | None
    resolved: dict[str, Any] | None
    scanned: list[dict[str, Any]]
    dateline: str | None
    opening: str | None
    weekday: str | None
    received: str | None
    confidence: float

    @classmethod
    def of(cls, data: dict[str, Any]) -> Reading:
        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            anchor_kind=str(data.get("anchor_kind") or "head"),
            date_written=data.get("date_written"),
            resolved=data.get("date_written_resolved"),
            scanned=list(data.get("dateline_dates") or []),
            dateline=data.get("dateline"),
            opening=data.get("opening"),
            weekday=(data.get("weekday") or None) and str(data["weekday"]).lower(),
            received=data.get("received_date"),
            confidence=confidence,
        )


@dataclass
class Decision:
    """A letter's date, or the reason it has none yet."""

    day: Day | None = None
    hold: str | None = None
    flags: list[str] = field(default_factory=list)
    caps: list[float] = field(default_factory=list)
    date_source: str | None = None
    year_inferred_from: list[str] = field(default_factory=list)
    readers: str | None = None  # agree | disagree | single
    deviation_days: int | None = None
    candidate: Day | None = None
    received: Day | None = None
    confidence: float = 0.0
    #: Pending: a yearless reading's (month, day), dated in the second pass.
    yearless: tuple[int, int] | None = None

    @property
    def materialized(self) -> bool:
        return self.day is not None and self.hold is None

    def held(self, reason: str, candidate: Day | None = None) -> Decision:
        self.hold = reason
        self.candidate = candidate or self.candidate or self.day
        self.day = None
        return self


def decide(
    readings: list[Reading],
    *,
    tolerance_days: int | None,
    strict_single_reader: bool = False,
    reviewed: dict[int, Day] | None = None,
) -> list[Decision]:
    """One decision per reading, in volume order.

    *reviewed* maps a reading's index to a date a reviewer accepted; it wins
    over everything and is flagged as such. *tolerance_days* is how far from
    its neighbours' median a printed date may sit before it is held; ``None``
    leaves chronology unchecked (each decision still reports its deviation).
    """
    reviewed = reviewed or {}
    decisions = [_read_one(r, strict_single_reader) for r in readings]
    _check_chronology(decisions, tolerance_days)
    _infer_years(decisions, readings)
    for index, (reading, decision) in enumerate(zip(readings, decisions, strict=True)):
        if index in reviewed:
            decision.day = reviewed[index]
            decision.hold = None
            decision.flags.append("reviewed")
            decision.caps = [CAP_REVIEWED]
            decision.date_source = "reviewer"
            decision.confidence = CAP_REVIEWED
        elif decision.materialized:
            decision.confidence = min([max(reading.confidence, 0.0), CAP_DEFAULT, *decision.caps])
        if decision.materialized and reading.received:
            decision.received = _received(reading.received, decision.day)
    return decisions


def _read_one(reading: Reading, strict_single_reader: bool) -> Decision:
    decision = Decision(date_source=reading.anchor_kind)
    written = reading.date_written
    if reading.anchor_kind == "undated" or not (written and written.strip()):
        return decision.held(UNDATED)
    if _CONJECTURE.search(written) or _CONJECTURE.search(reading.dateline or ""):
        return decision.held(CONJECTURAL)
    if reading.anchor_kind == "editorial":
        decision.caps.append(CAP_EDITORIAL)

    year = year_in(written)
    if year is None:
        md = month_day(written)
        if md is None:
            return decision.held(UNREADABLE)
        decision.yearless = md
        return decision

    model = from_resolved(reading.resolved) or read_day(written)
    if model is None:
        return decision.held(UNREADABLE)
    decision.day = model

    printed = f"{reading.dateline or ''} {reading.opening or ''}"
    if str(year) not in printed:
        return decision.held(HALLUCINATED_YEAR, candidate=model)

    scanned = next(
        (d for d in map(from_resolved, reading.scanned) if d is not None and d.precision == "day"),
        None,
    )
    if scanned is not None:
        same = (
            scanned.start == model.start
            if model.precision == "day"
            else (scanned.start.year, scanned.start.month) == (model.start.year, model.start.month)
            if model.precision == "month"
            else scanned.start.year == model.start.year
        )
        if not same:
            decision.readers = "disagree"
            return decision.held(READER_DISAGREEMENT, candidate=model)
        decision.readers = "agree"
    else:
        decision.readers = "single"
        if strict_single_reader:
            return decision.held(SINGLE_READER_STRICT, candidate=model)
        decision.flags.append("single_reader")
        decision.caps.append(CAP_SINGLE_READER)

    if reading.weekday and model.precision == "day" and weekday_of(model.start) != reading.weekday:
        decision.flags.append("weekday_mismatch")
        decision.caps.append(CAP_WEEKDAY_MISMATCH)
    return decision


def _check_chronology(decisions: list[Decision], tolerance_days: int | None) -> None:
    """Hold a printed date that breaks the order its neighbours set.

    A collected edition prints letters roughly in order, so a letter belongs
    between the letters around it: on or after the median of the (up to three)
    dated letters before it, on or before the median of those after. A letter
    with neighbours on one side only is bounded on that side only. Its deviation
    is how many days it falls outside that window; beyond *tolerance_days* it is
    held. Measuring against the window rather than a single median keeps the
    first letter of a sequence, and a sparse stretch where letters are years
    apart, from looking out of place.
    """
    # Every deviation is measured against the dates as printed, before any is
    # held: holding one letter must not change the verdict on its neighbours.
    dated = [i for i, d in enumerate(decisions) if d.materialized]
    days = {i: decisions[i].day.start.toordinal() for i in dated}
    conflicts: list[int] = []
    for position, index in enumerate(dated):
        before = [days[i] for i in dated[max(0, position - NEIGHBOURS) : position]]
        after = [days[i] for i in dated[position + 1 : position + 1 + NEIGHBOURS]]
        if not before and not after:
            continue
        medians = [statistics.median(side) for side in (before, after) if side]
        low = min(medians) if before else None
        high = max(medians) if after else None
        if low is not None and high is not None and low > high:
            low, high = high, low
        day = days[index]
        deviation = 0
        if low is not None and day < low:
            deviation = int(low - day)
        elif high is not None and day > high:
            deviation = int(day - high)
        decisions[index].deviation_days = deviation
        if tolerance_days is not None and deviation > tolerance_days:
            conflicts.append(index)
    for index in conflicts:
        decisions[index].held(CHRONOLOGY_CONFLICT)


def _infer_years(decisions: list[Decision], readings: list[Reading]) -> None:
    """Date each yearless reading forward from the dated letter before it."""
    for index, decision in enumerate(decisions):
        if decision.yearless is None or decision.hold is not None:
            continue
        previous = next(
            (decisions[i] for i in range(index - 1, -1, -1) if _anchors(decisions[i])), None
        )
        following = next(
            (decisions[i] for i in range(index + 1, len(decisions)) if _anchors(decisions[i])),
            None,
        )
        if previous is None or following is None:
            decision.held(YEAR_UNBRACKETED)
            continue
        candidate = forward_from(previous.day.start, *decision.yearless)
        if candidate is None:
            decision.held(UNREADABLE)
            continue
        if candidate.start > following.day.start:
            decision.held(YEAR_UNBRACKETED, candidate=candidate)
            continue
        weekday = readings[index].weekday
        if weekday and weekday_of(candidate.start) != weekday:
            decision.held(WEEKDAY_CONFLICT, candidate=candidate)
            continue
        decision.day = candidate
        decision.year_inferred_from = ["previous_letter", "next_letter"]
        decision.caps.append(CAP_INFERRED_YEAR)


def _anchors(decision: Decision) -> bool:
    """A letter whose own printed year may date its yearless neighbours."""
    return decision.materialized and decision.yearless is None


def _received(text: str, written: Day) -> Day | None:
    """A receipt note, read forward from the letter's own date."""
    if (explicit := read_day(text)) is not None:
        return explicit
    md = month_day(text)
    if md is None:
        return None
    return forward_from(written.start, *md)


def format_day(day: Day | None) -> str:
    if day is None:
        return "undated"
    if day.precision == "day":
        return f"{day.start.day} {calendar.month_name[day.start.month]} {day.start.year}"
    if day.precision == "month":
        return f"{calendar.month_name[day.start.month]} {day.start.year}"
    return str(day.start.year)


def event_span(day: Day) -> tuple[str, str]:
    """ISO timestamps covering the whole span, its last day inclusive.

    The engine's day-span convention: midnight to 23:59:59 UTC.
    """
    return (
        f"{day.start.isoformat()}T00:00:00+00:00",
        f"{day.end.isoformat()}T23:59:59+00:00",
    )
