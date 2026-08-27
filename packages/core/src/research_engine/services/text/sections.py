"""Recover a section table from markdown headings.

Docling converts PDFs, DOCX and the rest by exporting a `DoclingDocument` to
markdown, and its headings survive that export as ordinary `#` lines. That is
the cheap seam: rather than walking the `DoclingDocument` tree and then hunting
each node's prose in the exported string, read the structure back out of the
markdown that is already the canonical text. The offsets are then exact by
construction, because they are offsets into the very string being scanned.

The output is the same section table an EPUB produces — boundaries, heading and
level — so every consumer downstream is indifferent to which parser it came
from.
"""

from __future__ import annotations

import re

from research_engine.services.ingestion.chunking.fixed_window import trim_span

#: ATX headings only. Setext (`===` underlines) is not emitted by Docling and is
#: ambiguous with horizontal rules and table borders, which would cost more in
#: false sections than it could recover in real ones.
_HEADING = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*$", re.MULTILINE)


def sections_from_markdown(text: str) -> list[dict]:
    """Section boundaries for each markdown heading in *text*, in document order.

    A section runs from its own heading line to the start of the next heading of
    *any* level, or to the end of the text. Sections are therefore disjoint: a
    chapter's span holds only the prose before its first subsection, not the
    subsections themselves. That is deliberate — it reports what the author put
    under each heading, and ``build_node_tree`` widens parents to cover their
    descendants once nesting is known from ``level``. Splitting the two keeps
    this function a straight read of the text, with no view about hierarchy.

    Prose before the first heading gets no section. It is not lost — the root
    node of a document tree spans the whole text — and inventing an untitled
    sibling for it would put a node with nothing to say beside real chapters.
    """
    matches = list(_HEADING.finditer(text))
    if not matches:
        return []

    sections: list[dict] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        start, end = trim_span(text, start, end)
        if start >= end:
            continue
        sections.append(
            {
                "char_start": start,
                "char_end": end,
                "heading": match.group(2).strip(),
                "level": len(match.group(1)),
            }
        )
    return sections


#: Chapter words, in order. Roman numerals and arabic digits are handled
#: separately; these are the books that spell it out.
_CHAPTER_WORDS = {
    word: value
    for value, word in enumerate(
        [
            "one", "two", "three", "four", "five", "six", "seven", "eight",
            "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
            "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
            "twenty", "twentyone", "twentytwo", "twentythree", "twentyfour",
            "twentyfive",
        ],
        start=1,
    )
}

_ROMAN = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
    "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
    "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20, "xxi": 21,
    "xxii": 22, "xxiii": 23, "xxiv": 24, "xxv": 25,
}

_CHAPTER = re.compile(r"^[ \t]*(?:CHAPTER|Chapter)[ \t]+([A-Za-z0-9]+)[ \t]*.*$", re.MULTILINE)

#: A heading is a short line. Past this it is a sentence that happens to begin
#: with the word, which is common in books *about* books.
_MAX_HEADING_CHARS = 80

#: Fewer than this is not a sequence, it is a coincidence.
_MIN_CHAPTERS = 3

#: Chapters have a book's worth of text between them. This is the rule that
#: separates a chapter *start* from a chapter *mention*, which is the whole
#: difficulty: a contents list, a back-of-book index and a notes section are all
#: perfect ascending runs whose entries sit a few dozen characters apart.
#: Measured over the trade books in this corpus, real chapters are 24k-105k
#: characters apart and the false runs are 30-3,400.
_MIN_MEDIAN_GAP = 5_000

#: How far through the text the last chapter must fall. A sequence that stops
#: early mislabels everything after it, because a section runs to the next
#: heading or to the end of the text — one book's chapters break at 8 of 27 on a
#: stray reference list, and chapter 8 would otherwise be titled over the
#: remaining 63% of the book. Better no structure than wrong structure.
_MUST_REACH = 0.55


def _chapter_number(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        return int(token)
    return _CHAPTER_WORDS.get(token) or _ROMAN.get(token)


def _ascending_runs(text: str) -> list[list[tuple[int, int, str]]]:
    """Chapter matches split into maximal ascending runs of (number, offset, line).

    Ascent is what separates the body from the front matter: a contents list and
    the chapters it lists are both numbered from one, so they form two runs
    rather than one confused sequence.
    """
    hits: list[tuple[int, int, str]] = []
    for match in _CHAPTER.finditer(text):
        line = text[match.start() : match.end()].strip()
        if len(line) > _MAX_HEADING_CHARS:
            continue
        number = _chapter_number(match.group(1))
        if number is None:
            continue
        hits.append((number, match.start(), line))

    runs: list[list[tuple[int, int, str]]] = []
    current: list[tuple[int, int, str]] = []
    for hit in hits:
        if current and hit[0] <= current[-1][0]:
            runs.append(current)
            current = []
        current.append(hit)
    if current:
        runs.append(current)
    return runs


def _join_continuing_runs(runs: list[list]) -> list[list]:
    """Rejoin runs that continue each other's numbering.

    One backward reference mid-book splits a real sequence in two: a list naming
    chapters 4-8 sits between chapter 8 and chapter 9, so the ascent breaks and
    one book looks like two shorter ones.
    """
    joined: list[list] = []
    for run in runs:
        if joined and run[0][0] == joined[-1][-1][0] + 1:
            joined[-1] = joined[-1] + run
        else:
            joined.append(run)
    return joined


def _median_gap(run: list[tuple[int, int, str]]) -> float:
    if len(run) < 2:
        return 0.0
    gaps = sorted(run[index + 1][1] - run[index][1] for index in range(len(run) - 1))
    return gaps[len(gaps) // 2]


def sections_from_chapters(text: str) -> list[dict]:
    """Section boundaries from `Chapter N` lines in otherwise unmarked prose.

    Library and e-reader exports arrive as flat text: no markdown, no tags, and
    a structure layer that is therefore a single root node covering the whole
    book. The chapter headings are still in the prose, and they are the only
    structural signal these files carry.

    The difficulty is not finding `Chapter N` — it is that a contents list, a
    back-of-book index, a notes section and a reference list all contain it, and
    look identical to a chapter start line by line. Every rule here exists to
    separate a chapter *start* from a chapter *mention*, and each was added
    because it was measured to be necessary on a real book in this corpus.

    Returns the same shape as `sections_from_markdown`, so downstream callers
    cannot tell which produced it. Returns nothing rather than guessing: on the
    sixteen plain-text books measured, six yield a sequence and ten correctly
    yield none.
    """
    candidates = [
        run
        for run in _join_continuing_runs(_ascending_runs(text))
        if len(run) >= _MIN_CHAPTERS and _median_gap(run) >= _MIN_MEDIAN_GAP
    ]
    if not candidates:
        return []

    # Widest, not longest: a back-of-book index of 27 entries is a longer run
    # than the 8 real chapters it lists, and spans 0.5% of the text.
    chosen = max(candidates, key=lambda run: run[-1][1] - run[0][1])
    if chosen[-1][1] < _MUST_REACH * len(text):
        return []

    sections: list[dict] = []
    for index, (_number, offset, line) in enumerate(chosen):
        end = chosen[index + 1][1] if index + 1 < len(chosen) else len(text)
        start, end = trim_span(text, offset, end)
        if start >= end:
            continue
        sections.append(
            {"char_start": start, "char_end": end, "heading": line, "level": 1}
        )
    return sections
