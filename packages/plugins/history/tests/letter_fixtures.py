"""Readings of real datelines from *Life and Letters of Faraday*, vol 1.

Each record is shaped as core stores it: the model's fields, plus what core's
enricher adds — ``date_written_resolved`` from the model's reading and
``dateline_dates`` from the scanner's independent reading of the verbatim
dateline. Both are computed here with core's own functions, so the fixtures
cannot drift from what extraction would actually produce.
"""

from __future__ import annotations

from typing import Any

from research_engine.services.text.dates import parse_fuzzy_date, scan_dates


def _json(date: Any) -> dict[str, str] | None:
    if date is None:
        return None
    return {
        "start": date.start.isoformat(),
        "end": date.end.isoformat(),
        "precision": str(date.precision),
    }


def opening_data(
    opening: str,
    *,
    dateline: str | None = None,
    date_written: str | None = None,
    anchor_kind: str = "head",
    weekday: str | None = None,
    place: str | None = None,
    received_date: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    confidence: float = 0.9,
    **extra: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "opening": opening,
        "anchor_kind": anchor_kind,
        "confidence": confidence,
    }
    for key, value in {
        "dateline": dateline,
        "date_written": date_written,
        "weekday": weekday,
        "place": place,
        "received_date": received_date,
        "sender": sender,
        "recipient": recipient,
    }.items():
        if value is not None:
            data[key] = value
    if dateline is not None:
        data["dateline_dates"] = [
            {**_json(d), "offset": start} for start, _end, d in scan_dates(dateline)
        ]
    if date_written is not None:
        data["date_written_resolved"] = _json(parse_fuzzy_date(date_written))
    data.update(extra)
    return data


#: r.n13 — Rome, 14 April 1814.
ROME_APRIL_1814 = opening_data(
    "FARADAT TO HIS MOTHER.",
    dateline="' Eome : April 14, 1814.",
    date_written="April 14, 1814",
    place="Rome",
    sender="Faraday",
    recipient="his mother",
)
#: r.n14 — no year printed, and a receipt note.
GENEVA_JULY_1 = opening_data(
    "FARADAY TO HIS MOTHER.",
    dateline="' Geneva : July 1. Received July 18.",
    date_written="July 1",
    place="Geneva",
    received_date="July 18",
    sender="Faraday",
    recipient="his mother",
    confidence=0.7,
)
#: r.n15 — 6 August 1814 was a Saturday, as printed.
GENEVA_AUGUST_6 = opening_data(
    "TO ME. E. G. ABBOTT.",
    dateline="' Geueva : Saturday, August 6, 1814. Received August 18.",
    date_written="August 6, 1814",
    weekday="saturday",
    place="Geneva",
    recipient="Mr. E. G. Abbott",
)
#: r.n22 / r.n24 / r.n26 — the rest of the tour.
ROME_JANUARY_1815 = opening_data(
    "TO MR. E. ABBOTT.",
    dateline="' Rome : January 12, 1815. Received February 5.",
    date_written="January 12, 1815",
    place="Rome",
)
ROME_FEBRUARY_1815 = opening_data(
    "FAEADAY TO IIUXTABLE.",
    dateline="' Eome : February 13, 1815.",
    date_written="February 13, 1815",
    place="Rome",
)
BRUSSELS_1815 = opening_data(
    "FARADAY TO HIS MOTHER.",
    dateline="' Bruxelles : April 16, 1815.",
    date_written="April 16, 1815",
    place="Brussels",
)
#: r.n23 — printed 1816, between letters of January and February 1815.
ROME_1816 = opening_data(
    "FARADAY TO HIS MOTHER.",
    dateline="' Rome : February 13, 1816.",
    date_written="February 13, 1816",
    place="Rome",
)
#: r.n70 — the dateline merged into the heading; 28 July 1822 was a Sunday.
MARINO = opening_data(
    "TO MRS. EAEADAT.",
    dateline="' Marino : Sunday, July 28, 1822.",
    date_written="July 28, 1822",
    weekday="sunday",
    place="Marino",
    recipient="Mrs. Faraday",
)
#: r.n9 — Faraday wrote Thursday; 9 April 1813 was a Friday.
APRIL_9_1813 = opening_data(
    "FARADAY TO ABBOTT.",
    dateline="'Thursday evening, April 9, 1813.",
    date_written="April 9, 1813",
    weekday="thursday",
)
#: r.n88 — French and day-first: only the model can read it.
AMPERE = opening_data(
    "m. ampere to faraday.",
    dateline="'Paris: 13 octobre 1830.",
    date_written="13 October 1830",
    place="Paris",
    sender="M. Ampère",
    recipient="Faraday",
)
#: r.n60 — the editor's own guess.
WOLLASTON = opening_data(
    "WOLLASTON TO FABADAY.",
    dateline="' October 31, or November 1. ' (Must have been about November 1.)",
    date_written="October 31, or November 1",
)
#: r.n55 — a note returning a key, with no date at all.
KEY_NOTE = opening_data(
    "FARADAY TO MISS SAKAH BARNARD.", anchor_kind="undated", confidence=0.6
)
#: r.n5 — the editor dates the letter in a sentence of narrative.
THIRD_TO_ABBOTT = opening_data(
    "His third letter to his friend Abbott is dated August 11, 1812.",
    dateline="His third letter to his friend Abbott is dated August 11, 1812.",
    date_written="August 11, 1812",
    anchor_kind="editorial",
    confidence=0.8,
)
