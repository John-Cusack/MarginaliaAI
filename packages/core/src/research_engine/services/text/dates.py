"""Read the dates people actually wrote in letters.

A schema can declare a field ``type: fuzzy_date`` — the history pack's
``epistolary_references`` does, for the date of a letter being referred to — and
until now that declaration did nothing at all. The value came back as whatever
string the model chose and was stored as that string, so ``find_missing_letters``
compared "the 15th ult." against a timeline and matched nothing.

The hard cases here are not ambiguity in general, they are the specific
conventions of nineteenth-century correspondence:

- **ult.** (*ultimo*) — the same day of the *previous* month.
- **inst.** (*instant*) — the *current* month.
- **prox.** (*proximo*) — the *next* month.

"Yours of the 3d ult." is a complete date to the man who wrote it and useless
without knowing when he wrote it. So those forms resolve only against an anchor,
and return ``None`` without one rather than inventing a year — a wrong date in a
timeline is worse than a gap, because a gap is visible.

Ordinals are period-spelled: ``2d`` and ``3d`` are as common as ``2nd`` and
``3rd``, and months are abbreviated ``Jany`` and ``Feby``. Both are accepted.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime

from research_engine.domain.common import DatePrecision
from research_engine.domain.events import FuzzyDate

MONTHS = {
    "january": 1, "jan": 1, "jany": 1, "janr": 1,
    "february": 2, "feb": 2, "feby": 2, "febr": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "sepr": 9,
    "october": 10, "oct": 10, "octr": 10,
    "november": 11, "nov": 11, "novr": 11,
    "december": 12, "dec": 12, "decr": 12,
}

#: Northern-hemisphere seasons, which is the right assumption for this corpus
#: and a wrong one to make silently for another. Winter is the awkward one: "the
#: winter of 1862" begins in December 1862 and ends in February 1863.
SEASONS = {
    "spring": (3, 1, 5, 31, 0),
    "summer": (6, 1, 8, 31, 0),
    "autumn": (9, 1, 11, 30, 0),
    "fall": (9, 1, 11, 30, 0),
    "winter": (12, 1, 2, 28, 1),
}

_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
_ORDINAL = r"(?:st|nd|rd|th|d)"

#: Leading words a model tends to carry into the field along with the date —
#: "yours of the 15th", "dated May 3d". Stripping them here means the schema
#: author does not have to fight the phrasing in the prompt.
_LEAD = re.compile(
    r"^\s*(?:your[s]?\s+of\s+|letter\s+of\s+|dated\s+|written\s+|on\s+|of\s+|the\s+)+",
    re.IGNORECASE,
)

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_NUMERIC = re.compile(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$")
_MONTH_DAY_YEAR = re.compile(
    rf"^({_MONTH_NAMES})\.?\s+(\d{{1,2}}){_ORDINAL}?\s*,?\s*(\d{{4}})$", re.IGNORECASE
)
_DAY_MONTH_YEAR = re.compile(
    rf"^(\d{{1,2}}){_ORDINAL}?\s+({_MONTH_NAMES})\.?\s*,?\s*(\d{{4}})$", re.IGNORECASE
)
_MONTH_YEAR = re.compile(rf"^({_MONTH_NAMES})\.?\s*,?\s*(\d{{4}})$", re.IGNORECASE)
_MONTH_DAY = re.compile(
    rf"^({_MONTH_NAMES})\.?\s+(\d{{1,2}}){_ORDINAL}?$", re.IGNORECASE
)
_DAY_MONTH = re.compile(
    rf"^(\d{{1,2}}){_ORDINAL}?\s+({_MONTH_NAMES})\.?$", re.IGNORECASE
)
_SEASON = re.compile(
    r"^(spring|summer|autumn|fall|winter)\s+(?:of\s+)?(\d{4})$", re.IGNORECASE
)
_DECADE = re.compile(r"^(\d{3})0\s*'?s$", re.IGNORECASE)
_YEAR = re.compile(r"^(\d{4})$")
_WEEK_OF = re.compile(r"^week\s+of\s+(.+)$", re.IGNORECASE)

#: ult./inst./prox. and their long forms, with the month offset each implies.
_RELATIVE_MONTH = re.compile(
    r"^(\d{1,2})" + _ORDINAL + r"?\s*"
    r"(ult(?:o|imo)?|inst(?:\.|ant)?|prox(?:o|imo)?)\.?$",
    re.IGNORECASE,
)
_BARE_DAY = re.compile(rf"^(\d{{1,2}}){_ORDINAL}$")

_OFFSET = {"ult": -1, "inst": 0, "prox": 1}


def parse_fuzzy_date(
    text: str, *, relative_to: datetime | None = None
) -> FuzzyDate | None:
    """Parse a written date into a span and a precision, or return None.

    *relative_to* is the date of the document the phrase was written in. Forms
    that name only a day — "the 15th", "the 3d ult." — mean nothing without it,
    and are refused rather than guessed.
    """
    if not text or not text.strip():
        return None

    cleaned = _LEAD.sub("", text.strip()).strip().rstrip(",.")
    if not cleaned:
        return None

    if match := _WEEK_OF.match(cleaned):
        start = parse_fuzzy_date(match.group(1), relative_to=relative_to)
        if start is None:
            return None
        return _span(start.start, _add_days(start.start, 6), DatePrecision.week)

    for parser in (
        _try_iso,
        _try_numeric,
        _try_month_day_year,
        _try_day_month_year,
        _try_season,
        _try_month_year,
        _try_decade,
        _try_year,
    ):
        if result := parser(cleaned):
            return result

    # Everything below needs to know when the letter was written.
    if relative_to is None:
        return None
    return _try_relative(cleaned, relative_to)


def _try_iso(text: str) -> FuzzyDate | None:
    if match := _ISO.match(text):
        year, month, day = (int(g) for g in match.groups())
        return _day(year, month, day)
    return None


def _try_numeric(text: str) -> FuzzyDate | None:
    """``5/15/1862``. Read month-first, which is the American convention.

    This is the one genuinely ambiguous form — 5/6/1862 is two dates — and
    nothing in the string resolves it. Month-first is right for this corpus and
    would be wrong for a British one, so it is stated rather than assumed.
    """
    if match := _NUMERIC.match(text):
        month, day, year = (int(g) for g in match.groups())
        return _day(year, month, day)
    return None


def _try_month_day_year(text: str) -> FuzzyDate | None:
    if match := _MONTH_DAY_YEAR.match(text):
        month, day, year = MONTHS[match.group(1).lower()], int(match.group(2)), int(match.group(3))
        return _day(year, month, day)
    return None


def _try_day_month_year(text: str) -> FuzzyDate | None:
    if match := _DAY_MONTH_YEAR.match(text):
        day, month, year = int(match.group(1)), MONTHS[match.group(2).lower()], int(match.group(3))
        return _day(year, month, day)
    return None


def _try_month_year(text: str) -> FuzzyDate | None:
    if match := _MONTH_YEAR.match(text):
        month, year = MONTHS[match.group(1).lower()], int(match.group(2))
        return _month(year, month)
    return None


def _try_season(text: str) -> FuzzyDate | None:
    if match := _SEASON.match(text):
        start_month, start_day, end_month, end_day, year_shift = SEASONS[
            match.group(1).lower()
        ]
        year = int(match.group(2))
        end_year = year + year_shift
        if end_month == 2:
            end_day = calendar.monthrange(end_year, 2)[1]
        return _span(
            _at(year, start_month, start_day),
            _end_of(end_year, end_month, end_day),
            DatePrecision.season,
        )
    return None


def _try_decade(text: str) -> FuzzyDate | None:
    if match := _DECADE.match(text):
        start_year = int(match.group(1)) * 10
        return _span(
            _at(start_year, 1, 1),
            _end_of(start_year + 9, 12, 31),
            DatePrecision.decade,
        )
    return None


def _try_year(text: str) -> FuzzyDate | None:
    if match := _YEAR.match(text):
        year = int(match.group(1))
        return _span(_at(year, 1, 1), _end_of(year, 12, 31), DatePrecision.year)
    return None


def _try_relative(text: str, anchor: datetime) -> FuzzyDate | None:
    """The epistolary forms, resolved against the letter's own date."""
    if match := _RELATIVE_MONTH.match(text):
        day = int(match.group(1))
        keyword = match.group(2).lower().rstrip(".")
        offset = next(
            months for prefix, months in _OFFSET.items() if keyword.startswith(prefix)
        )
        year, month = _shift_month(anchor.year, anchor.month, offset)
        return _day(year, month, day)

    if match := _BARE_DAY.match(text):
        # "the 15th" with no month named is the current month by convention —
        # and if that day has not arrived yet when the letter was written, the
        # writer meant last month.
        day = int(match.group(1))
        year, month = anchor.year, anchor.month
        if day > anchor.day:
            year, month = _shift_month(year, month, -1)
        return _day(year, month, day)

    if match := _MONTH_DAY.match(text):
        month, day = MONTHS[match.group(1).lower()], int(match.group(2))
        return _day(_year_for(anchor, month), month, day)

    if match := _DAY_MONTH.match(text):
        day, month = int(match.group(1)), MONTHS[match.group(2).lower()]
        return _day(_year_for(anchor, month), month, day)

    return None


def _year_for(anchor: datetime, month: int) -> int:
    """A month with no year is the most recent occurrence of it."""
    return anchor.year if month <= anchor.month else anchor.year - 1


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + offset
    return index // 12, index % 12 + 1


def _day(year: int, month: int, day: int) -> FuzzyDate | None:
    if not 1 <= month <= 12:
        return None
    last = calendar.monthrange(year, month)[1]
    if not 1 <= day <= last:
        # "the 31st" of a thirty-day month is a transcription error or an OCR
        # one. Refused, for the same reason a day-only form without an anchor is.
        return None
    return _span(_at(year, month, day), _end_of(year, month, day), DatePrecision.day)


def _month(year: int, month: int) -> FuzzyDate | None:
    if not 1 <= month <= 12:
        return None
    last = calendar.monthrange(year, month)[1]
    return _span(
        _at(year, month, 1), _end_of(year, month, last), DatePrecision.month
    )


def _at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _end_of(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 23, 59, 59, tzinfo=UTC)


def _add_days(moment: datetime, days: int) -> datetime:
    from datetime import timedelta

    end = moment + timedelta(days=days)
    return end.replace(hour=23, minute=59, second=59)


def _span(start: datetime, end: datetime, precision: DatePrecision) -> FuzzyDate:
    return FuzzyDate(start=start, end=end, precision=precision)


# --- Finding dates in text that has been through a scanner -------------------

#: A month word, a day, and a year, with the noise a page scan leaves behind.
#: The year may be bracketed (an editor supplying what the writer omitted),
#: slashed and abbreviated ("April 18/61"), or plain.
#: A month word and a day. What follows them is handled separately, because
#: what follows is where the noise lives.
_MONTH_AND_DAY = re.compile(
    r"\b([A-Za-z]{3,9})\.?"                 # month word, possibly misread
    r"[\s\]\)\[\(\|,]{0,4}"                  # an editor's bracket may close here
    r"(\d{1,2})(?:st|nd|rd|th|d)?\.?"       # day
)

#: A four-digit year, looked for just past the day.
_FULL_YEAR = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")

#: "April 18/61". Only after a slash: a bare two-digit number following a day is
#: far more often an hour than a year.
_SLASHED_YEAR = re.compile(r"^[\s\]\)\[\(\|,]{0,3}/\s?(\d{2})\b")

#: How far past the day to look for the year. Telegram datelines put a time
#: between the two — "March 24 11 am 1862" — and an editor may insert an
#: alternative day — "Aug 9 [10] 1861". Both need stepping over. Kept short so
#: the year cannot be picked out of the letter's first sentence.
_YEAR_WINDOW = 22

#: Misreads frequent enough in scanned nineteenth-century print to name. The
#: similarity fallback below catches the rest, but "Dee" for "Dec" shares only
#: two letters with its target and sits just under any threshold loose enough
#: to be safe.
_MONTH_OCR = {
    "dee": 12, "deo": 12, "dec'": 12,
    "jime": 6, "jnne": 6, "jnue": 6,
    "jnly": 7, "julv": 7,
    "angust": 8, "augnst": 8,
    "marcli": 3, "mareh": 3, "mareli": 3,
    "febmary": 2, "febrnary": 2,
    "jaimary": 1, "jannary": 1,
    "aprii": 4, "apl": 4, "apnl": 4,
    "oet": 10, "octr": 10,
    "xov": 11, "novr": 11,
    "sepr": 9, "sepc": 9,
}

#: How close a scanned word has to be to a month name to be read as one. A page
#: scan turns "Dec" into "Dee" and "March" into "Marcli" often enough that
#: listing the misreads is a losing game; requiring a day number immediately
#: after is what keeps this from matching ordinary prose.
_MONTH_SIMILARITY = 0.75


def _read_month(word: str) -> int | None:
    """A month number from a word a scanner may have damaged."""
    cleaned = word.strip(".").lower()
    if cleaned in MONTHS:
        return MONTHS[cleaned]
    if cleaned in _MONTH_OCR:
        return _MONTH_OCR[cleaned]
    import difflib

    close = difflib.get_close_matches(
        cleaned, MONTHS.keys(), n=1, cutoff=_MONTH_SIMILARITY
    )
    return MONTHS[close[0]] if close else None


def _read_year(digits: str, century: int | None) -> int | None:
    """Expand a two-digit year, or refuse when nothing says which century.

    "April 18/61" is 1861 in this corpus and 1961 in another. The century comes
    from the document the date was found in — the years it states in full — so
    the guess is drawn from the same page rather than from an assumption.
    """
    if len(digits) == 4:
        return int(digits)
    if len(digits) == 2 and century is not None:
        return century + int(digits)
    return None


def dominant_century(text: str) -> int | None:
    """The century this text is written about, from the years it spells out.

    Returns e.g. 1800 when four-digit years in the text are mostly 18xx.
    """
    years = [int(y) for y in re.findall(r"\b(1[5-9]\d{2}|20\d{2})\b", text)]
    if len(years) < 5:
        return None
    centuries: dict[int, int] = {}
    for year in years:
        centuries[year // 100 * 100] = centuries.get(year // 100 * 100, 0) + 1
    best = max(centuries.items(), key=lambda kv: kv[1])
    return best[0] if best[1] >= len(years) / 2 else None


def scan_dates(
    text: str, *, century: int | None = None
) -> list[tuple[int, int, FuzzyDate]]:
    """Every date this text states, as ``(start, end, date)`` in document order.

    Unlike :func:`parse_fuzzy_date`, which reads a string that is supposed to be
    a date, this looks for dates inside text that is mostly something else — a
    letter's dateline in among its address and salutation.

    A four-digit year anywhere just past the day wins over a two-digit number
    immediately after it. That ordering is what separates "March 24 11 am 1862"
    from a date in 1811: the hour sits exactly where a short year would.
    """
    found: list[tuple[int, int, FuzzyDate]] = []
    for match in _MONTH_AND_DAY.finditer(text):
        month = _read_month(match.group(1))
        if month is None:
            continue
        tail = text[match.end() : match.end() + _YEAR_WINDOW]
        if full := _FULL_YEAR.search(tail):
            year, end = int(full.group(1)), match.end() + full.end()
        elif short := _SLASHED_YEAR.match(tail):
            resolved = _read_year(short.group(1), century)
            if resolved is None:
                continue
            year, end = resolved, match.end() + short.end()
        else:
            continue
        parsed = _day(year, month, int(match.group(2)))
        if parsed is not None:
            found.append((match.start(), end, parsed))
    return found
