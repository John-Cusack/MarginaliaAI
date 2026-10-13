"""USFM Bibles: eBible.org's King James Version, chapter by chapter.

The archive holds one USFM file per book. What the 1769 text prints is kept,
verbatim; what eBible added for study is not:

- **Verses** (`\\v`) are units. Strong's tagging (`\\w word|strong="…"\\w*`)
  keeps the word and drops the number; the divine name (`\\nd`), the words
  of Jesus (`\\wj`) and transliterated letters (`\\tl`) keep their words and
  lose the styling. The printed pilcrow (¶) stays.
- **Supplied words** (`\\add … \\add*`, the KJV's italics) stay in the text
  and become `supplied` marks over exactly those characters.
- **Printed text outside the verse numbering** is kept as units of its own
  kind, so the verse count stays the one the edition is known by: psalm
  titles (`\\d`, kind `title`), the acrostic headings of Psalm 119 (`\\s1`,
  kind `heading`) and the subscriptions after Paul's letters (`\\s1` after the
  last verse, kind `subscription`).
- **Footnotes** (`\\f … \\f*`) are the translators' margin, not the text, and
  are dropped; so is the front matter (`\\id`, `\\h`, `\\toc`, `\\mt`).

Whitespace runs collapse to one space — USFM line layout carries no meaning —
and a marker this reader does not know fails the parse rather than leaking
into the text.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import TYPE_CHECKING, ClassVar

from dilemma.sources import register
from research_engine_sdk.units import Unit, UnitDocument, UnitMark

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dilemma.fetch import Stored
    from dilemma.sources import AcquireContext

#: USFM book codes of the 66-book protocanon, in order, with English names.
BOOKS: dict[str, str] = {
    "GEN": "Genesis", "EXO": "Exodus", "LEV": "Leviticus", "NUM": "Numbers",
    "DEU": "Deuteronomy", "JOS": "Joshua", "JDG": "Judges", "RUT": "Ruth",
    "1SA": "1 Samuel", "2SA": "2 Samuel", "1KI": "1 Kings", "2KI": "2 Kings",
    "1CH": "1 Chronicles", "2CH": "2 Chronicles", "EZR": "Ezra", "NEH": "Nehemiah",
    "EST": "Esther", "JOB": "Job", "PSA": "Psalms", "PRO": "Proverbs",
    "ECC": "Ecclesiastes", "SNG": "Song of Solomon", "ISA": "Isaiah",
    "JER": "Jeremiah", "LAM": "Lamentations", "EZK": "Ezekiel", "DAN": "Daniel",
    "HOS": "Hosea", "JOL": "Joel", "AMO": "Amos", "OBA": "Obadiah", "JON": "Jonah",
    "MIC": "Micah", "NAM": "Nahum", "HAB": "Habakkuk", "ZEP": "Zephaniah",
    "HAG": "Haggai", "ZEC": "Zechariah", "MAL": "Malachi",
    "MAT": "Matthew", "MRK": "Mark", "LUK": "Luke", "JHN": "John", "ACT": "Acts",
    "ROM": "Romans", "1CO": "1 Corinthians", "2CO": "2 Corinthians",
    "GAL": "Galatians", "EPH": "Ephesians", "PHP": "Philippians",
    "COL": "Colossians", "1TH": "1 Thessalonians", "2TH": "2 Thessalonians",
    "1TI": "1 Timothy", "2TI": "2 Timothy", "TIT": "Titus", "PHM": "Philemon",
    "HEB": "Hebrews", "JAS": "James", "1PE": "1 Peter", "2PE": "2 Peter",
    "1JN": "1 John", "2JN": "2 John", "3JN": "3 John", "JUD": "Jude",
    "REV": "Revelation",
}

#: Paragraph markers that frame a book rather than carry its text.
FRAME = frozenset({"id", "ide", "h", "toc1", "toc2", "toc3", "mt", "mt1", "mt2", "mt3"})
#: Paragraph markers that only lay text out (a paragraph, a poetic line, a
#: stanza break); the words they hold, if any, belong to the current unit.
LAYOUT = frozenset({"p", "q", "q1", "q2", "m", "b", "nb", "pi", "mi"})

_FOOTNOTE = re.compile(r"\\f\s.*?\\f\*", re.DOTALL)
_STRONGS = re.compile(r"\\(\+?)w\s+([^|\\]*?)(?:\|[^\\]*)?\\\1w\*")
_STYLE = re.compile(r"\\\+?(add|nd|wj|tl)(\*|\s)")
_LINE = re.compile(r"\\([a-z]+[0-9]*)(?:\s(.*))?$")


class UsfmError(ValueError):
    """The USFM holds something this reader does not know how to keep."""


@register
class EbibleUsfm:
    keys: ClassVar[tuple[str, ...]] = ("kjv",)
    parser: ClassVar[str] = "dilemma.usfm"
    parser_version: ClassVar[str] = "1"

    async def acquire(self, context: AcquireContext) -> list[Stored]:
        url = context.entry.acquire.url
        assert url, f"{context.entry.key} has no URL in the catalog"
        return [await context.fetcher.fetch(url, source_key=context.entry.key, ext="zip")]

    def parse(self, key: str, artifacts: list[Stored]) -> Iterable[UnitDocument]:
        (artifact,) = artifacts
        with zipfile.ZipFile(io.BytesIO(artifact.path.read_bytes())) as archive:
            texts: dict[str, str] = {}
            for name in sorted(archive.namelist()):
                if name.lower().endswith(".usfm"):
                    text = archive.read(name).decode("utf-8-sig")
                    match = re.match(r"\\id\s+(\w+)", text)
                    if match and match.group(1) in BOOKS:
                        texts[match.group(1)] = text
        missing = [code for code in BOOKS if code not in texts]
        if missing:
            raise UsfmError(f"the archive lacks books {missing}")
        for code, book in BOOKS.items():
            yield from chapters(key, book, texts[code])


def chapters(key: str, book: str, usfm: str) -> Iterable[UnitDocument]:
    """One document per chapter of one book's USFM."""
    text = _STRONGS.sub(lambda m: m.group(2), _FOOTNOTE.sub("", usfm))
    chapter = 0
    pending: list[tuple[str, str, str, list[str]]] = []  # (ref, label, kind, raw parts)
    headings = 0

    def document() -> UnitDocument:
        kinds = [kind for _, _, kind, _ in pending]
        if "subscription" in kinds[:-1]:
            raise UsfmError(f"{book} {chapter}: a subscription is not the chapter's last unit")
        return UnitDocument(
            title=f"{book} {chapter}",
            source=f"dilemma:{key}:{book}.{chapter}",
            units=tuple(_unit(ref, label, kind, raw) for ref, label, kind, raw in pending),
            metadata={"book": book, "chapter": chapter},
        )

    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        match = _LINE.match(line.strip())
        if match is None:
            if not pending:
                raise UsfmError(f"{book} line {number}: text outside any unit")
            pending[-1][3].append(line)
            continue
        marker, rest = match.group(1), match.group(2) or ""
        if marker in FRAME:
            continue
        if marker == "c":
            if chapter:
                yield document()
            chapter, pending, headings = int(rest.split()[0]), [], 0
        elif not chapter:
            raise UsfmError(f"{book} line {number}: \\{marker} before the first chapter")
        elif marker == "v":
            label, _, body = rest.partition(" ")
            pending.append((f"{book} {chapter}:{label}", label, "verse", [body]))
        elif marker == "d":
            pending.append((f"{book} {chapter}:title", "", "title", [rest]))
        elif marker == "s1":
            # Psalm 119's acrostic letters are transliterated (\tl); a line
            # after a letter's verses that is not one is its subscription.
            has_verses = any(kind == "verse" for _, _, kind, _ in pending)
            if has_verses and "\\tl" not in rest:
                pending.append((f"{book} {chapter}:subscription", "", "subscription", [rest]))
            else:
                headings += 1
                pending.append((f"{book} {chapter} heading {headings}", "", "heading", [rest]))
        elif marker in LAYOUT:
            if rest.strip():
                if not pending:
                    raise UsfmError(f"{book} {chapter}: text before the first verse")
                pending[-1][3].append(rest)
        else:
            raise UsfmError(f"{book} {chapter}: unknown paragraph marker \\{marker}")
    if chapter:
        yield document()


def _unit(ref: str, label: str, kind: str, raw: list[str]) -> Unit:
    text, supplied = plain(" ".join(raw))
    if not text:
        raise UsfmError(f"{ref}: no text")
    return Unit(
        ref=ref, label=label, kind=kind, text=text,
        marks=tuple(UnitMark("supplied", start, end) for start, end in supplied),
        locator={"ref": ref},
    )


def plain(raw: str) -> tuple[str, list[tuple[int, int]]]:
    """Strip character markup; return the text and the supplied-word ranges."""
    out: list[str] = []
    supplied: list[tuple[int, int]] = []
    opened: list[int] = []

    def emit(piece: str) -> None:
        for char in piece:
            if char.isspace():
                if not out or out[-1] == " ":
                    continue
                char = " "
            out.append(char)

    cursor = 0
    for match in _STYLE.finditer(raw):
        emit(raw[cursor:match.start()])
        cursor = match.end()
        if match.group(1) != "add":
            continue
        if match.group(2) == "*":
            if not opened:
                raise UsfmError(f"\\add* without \\add in {raw[:60]!r}")
            supplied.append((opened.pop(), len(out)))
        else:
            opened.append(len(out))
    emit(raw[cursor:])
    if opened:
        raise UsfmError(f"\\add without \\add* in {raw[:60]!r}")
    text = "".join(out).rstrip(" ")
    if "\\" in text:
        raise UsfmError(f"unknown character marker left in {text[:80]!r}")
    trimmed = []
    for start, end in supplied:
        end = min(end, len(text))
        while start < end and text[start] == " ":
            start += 1
        while end > start and text[end - 1] == " ":
            end -= 1
        if end > start:
            trimmed.append((start, end))
    return text, trimmed
