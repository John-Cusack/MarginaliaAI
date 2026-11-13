"""The independent checks (design 7.5): a failure marks a span or gaps the page.

They exist because agreement is not correctness. Three readers from the same
family of errors can agree on a wrong word, and the checks are the only thing
looking at the page that no reader produced: the layout, the dictionary, the
sequence of note numbers, a table column's number format, the book's own
confirmed vocabulary, the running head, and the reader's own chrome.

Each check returns spans in the text it was given, and the caller decides whether
a span is marked or the whole page is gapped. Nothing here reads a page or calls
a model: they are cheap, deterministic, and run on every page.

WP0's measurement used these from ``tools/pipeline_checks.py``; that module now
imports them from here, so there is one implementation rather than two.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

Box = tuple[float, float, float, float]

#: A word, for the vocabulary check: letters and the marks a book's words carry.
WORD = re.compile(r"[A-Za-z][A-Za-z'\u2019-]*")

#: What the reader draws on top of a page that is not the book: its own chrome.
#: A leak is a gap or a mark, never text (design 7.5).
CHROME_MARKERS: tuple[str, ...] = (
    "back to",
    "go to",
    "sync",
    "page not available",
    "website temporarily unavailable",
)

#: Note numbers are compared as integers; anything else in a note marker is part
#: of the text, not part of the sequence.
NOTE_NUMBER = re.compile(r"\[(\d{1,3})\]|\b(\d{1,3})\.")


def line_count_mismatch(
    layout_lines: Sequence[Box], per_line_text: Sequence[str], *, slack: int = 0
) -> tuple[tuple[int, int], ...]:
    """A region whose pixel lines and text lines disagree, per design 7.5.

    The layout says how many lines of text are drawn; the reading says how many
    lines it produced. One missing line is a line of the book nobody read, so the
    check reports the region's line indices that have no counterpart - and if the
    counts cannot be paired at all, the whole region is reported as one span.
    """
    drawn, read = len(layout_lines), len(per_line_text)
    if abs(drawn - read) <= slack:
        return ()
    if read == 0:
        return ((0, 0),)
    if drawn == 0:
        return ((0, len(per_line_text) - 1),)
    # Report the lines the reading has and the page does not, and vice versa.
    end = max(drawn, read) - 1
    return ((min(drawn, read), end),)


def note_sequence(
    numbers: Sequence[tuple[int, int, int]], *, resets_at: Iterable[int] = ()
) -> tuple[tuple[int, int], ...]:
    """Note numbers out of sequence, with a restart allowed only at a named boundary.

    Each entry is ``(span_start, span_end, number)`` in the text's coordinates. A
    gap or a repeat is a mark: a note number was read wrong, or a note was lost.
    Books do restart their numbering at a part boundary, and treating that as a
    violation would mark the second half of every book - so a restart is accepted
    when the caller names the offset as a reset, and reported when it does not.
    """
    out: list[tuple[int, int]] = []
    previous: int | None = None
    resets = set(resets_at)
    for start, end, number in numbers:
        if previous is not None and number != previous + 1:
            restarted = number == 1
            if not restarted or start not in resets:
                out.append((start, end))
        previous = number
    return tuple(out)


def table_number_formats(rows: Sequence[Sequence[str]], *, quorum: float = 0.6) -> tuple[tuple[int, int], ...]:
    """Cells whose number format differs from the column's majority.

    A table column is a promise about format: currency where currency is, a
    percent sign where it is, thousands separators everywhere or nowhere. A single
    cell that breaks the promise is far more likely to be a misread than an edit
    in the book, so it is marked rather than trusted.
    """
    out: list[tuple[int, int]] = []
    if not rows:
        return ()
    width = max(len(row) for row in rows)
    for column in range(width):
        shapes = [
            number_shape(row[column]) for row in rows if column < len(row) and row[column].strip()
        ]
        shapes = [shape for shape in shapes if shape]
        if len(shapes) < 3:
            continue
        counts: dict[str, int] = {}
        for shape in shapes:
            counts[shape] = counts.get(shape, 0) + 1
        majority, seen = max(counts.items(), key=lambda item: item[1])
        if seen / len(shapes) < quorum:
            continue
        for index, row in enumerate(rows):
            if column >= len(row):
                continue
            cell = row[column]
            shape = number_shape(cell)
            if shape and shape != majority:
                out.append((index, column))
    return tuple(out)


def number_shape(cell: str) -> str:
    """The format of a number, with its digits replaced by placeholders.

    ``1,234.50`` -> ``sddd.dd``; ``-1.5%`` -> ``-d.d%``. The shape is what a
    column repeats, so it is what a wrong cell breaks.
    """
    text = cell.strip()
    if not text or not any(character.isdigit() for character in text):
        return ""
    return re.sub(r"\d", "d", text)


def heading_vs_toc(
    heading: str, labels: Sequence[str], *, tolerance: int = 2
) -> tuple[tuple[int, int], ...]:
    """A heading that does not match any ``toc.json`` label.

    The table of contents is the book's own statement about its headings, and it
    survives a bad render because it arrives in `toc.json` rather than on the
    page. A heading that is not in it is either a heading the TOC does not carry
    or a misread - either way the span is marked, not confirmed.
    """
    candidate = normalize_heading(heading)
    if not candidate:
        return ()
    for label in labels:
        if normalize_heading(label) == candidate:
            return ()
    for label in labels:
        if edit_distance(normalize_heading(label), candidate) <= tolerance:
            return ()
    return ((0, len(heading)),)


def normalize_heading(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def edit_distance(left: str, right: str) -> int:
    """Levenshtein, iterative, for short strings: headings and note markers."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, lc in enumerate(left, start=1):
        current = [i]
        for j, rc in enumerate(right, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (lc != rc)))
        previous = current
    return previous[-1]


def chrome_leaks(text: str, markers: Iterable[str] = CHROME_MARKERS) -> tuple[tuple[int, int], ...]:
    """The reader's own words on the page, in the reading.

    ``Back to``, the sync chrome, the throttle page: text that is on the screen
    and not in the book. It is never confirmed and never enters the vocabulary.
    """
    lowered = text.lower()
    spans: list[tuple[int, int]] = []
    for marker in markers:
        start = lowered.find(marker)
        while start != -1:
            spans.append((start, start + len(marker)))
            start = lowered.find(marker, start + 1)
    return tuple(sorted(spans))


# --------------------------------------------------------------------------- #
# the checks WP0 already had, moved unchanged
# --------------------------------------------------------------------------- #

def book_vocabulary(texts: Iterable[str], *, minimum: int = 3) -> frozenset[str]:
    """Words the book itself uses at least ``minimum`` times, case-folded."""
    counts: Counter[str] = Counter()
    for text in texts:
        counts.update(word.casefold() for word in WORD.findall(text))
    return frozenset(word for word, count in counts.items() if count >= minimum)


def unmatched_words(text: str, vocabulary: frozenset[str], dictionary: frozenset[str] | None = None) -> tuple[tuple[int, int], ...]:
    """Spans of words the vocabulary (and the dictionary, if given) do not know.

    Case-folded lookup: a case-only change is *caught*, which is the point — the
    check is what tells `MARGIN` from `Margin` without reading the page.
    """
    spans: list[tuple[int, int]] = []
    for match in WORD.finditer(text):
        word = match.group(0).casefold()
        if word in vocabulary:
            continue
        if dictionary is not None and word in dictionary:
            continue
        spans.append((match.start(), match.end()))
    return tuple(spans)


def catches(
    unmatched: Sequence[tuple[int, int]],
    span: tuple[int, int],
) -> bool:
    """Whether a check span covers the word a planted error sits in.

    The planted span lies inside the word it replaced, so overlap is the test.
    """
    start, end = span
    return any(check_start <= start and end <= check_end for check_start, check_end in unmatched)


def line_texts(
    truth: str,
    run_words: Sequence[tuple[str, int]],
    line_count: int,
) -> tuple[list[int | None], tuple[str, ...]]:
    """Attribute each truth word to the line it sits on, and join them per line.

    The truth is assembled from the page's runs, so a SequenceMatcher over the two
    word sequences maps most words exactly; a word it cannot place stays None and
    is counted, never guessed. Returns (word → line, text per line).
    """
    from difflib import SequenceMatcher

    truth_words = WORD.findall(truth)
    lines: list[int | None] = [None] * len(truth_words)
    matcher = SequenceMatcher(None, truth_words, [word for word, _line in run_words], autojunk=False)
    for tag, i1, i2, j1, _j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(i2 - i1):
            lines[i1 + offset] = run_words[j1 + offset][1]
    per_line: list[list[str]] = [[] for _ in range(line_count)]
    for word, line in zip(truth_words, lines, strict=True):
        if line is not None and 0 <= line < line_count:
            per_line[line].append(word)
    return lines, tuple(" ".join(words) for words in per_line)


def coverage_failures(
    line_boxes: Sequence[tuple[float, float, float, float]],
    line_texts: Sequence[str],
    *,
    median_density: float,
    short_ratio: float = 0.6,
) -> tuple[int, ...]:
    """Indices of lines that carry no text, or far less than their ink implies."""
    failures: list[int] = []
    for index, (box, text) in enumerate(zip(line_boxes, line_texts, strict=False)):
        width = max(0.0, float(box[2]) - float(box[0]))
        characters = len(text.strip())
        if characters == 0:
            failures.append(index)
            continue
        if median_density > 0 and width > 0:
            expected = width * median_density
            if characters < short_ratio * expected:
                failures.append(index)
    return tuple(failures)


def character_density(word: str, box: tuple[float, float, float, float]) -> float:
    """Characters per pixel of width for one recognised word."""
    width = max(0.0, float(box[2]) - float(box[0]))
    return len(word.strip()) / width if width else 0.0


HYPHENS = {"-", "\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015"}
DASHES = {"\u2013", "\u2014", "\u2015"}


def dashed(text: str) -> tuple[tuple[int, int], ...]:
    """Spans of dash characters that are not hyphens between a word's parts.

    A dash inside a word is a hyphen; one standing between words is a dash, and a
    reader that turns a dash into a hyphen (or the reverse) has changed the text
    without a visible cue in a diff.
    """
    span: list[tuple[int, int]] = []
    for match in re.finditer("[" + "".join(sorted(HYPHENS)) + "]", text):
        before = text[match.start() - 1] if match.start() else " "
        after = text[match.end()] if match.end() < len(text) else " "
        between_words = before.isspace() or after.isspace()
        if between_words and match.group(0) in DASHES:
            span.append((match.start(), match.end()))
    return tuple(span)


def implausible_numbers(text: str, *, min_digits: int = 3) -> tuple[tuple[int, int], ...]:
    """Numbers a book page would not print: runs of digits longer than a number is.

    A digit run of more than ``min_digits + 6`` characters is almost certainly a
    reader joining separate numbers (or the page's own label into the text); the
    check reports the span so it can be marked rather than stored.
    """
    span: list[tuple[int, int]] = []
    for match in re.finditer(r"\d[\d,.\u2013-]*", text):
        digits = sum(1 for character in match.group(0) if character.isdigit())
        if digits > min_digits + 6:
            span.append((match.start(), match.end()))
    return tuple(span)


def running_head_word(text: str, *, head_words: Sequence[str]) -> tuple[tuple[int, int], ...]:
    """Spans where the candidate repeats the book's running head.

    The header is chrome the candidate is not supposed to carry; a line that
    repeats it is either a capture mistake or a misread line, and either way it
    must not enter the text.
    """
    spans: list[tuple[int, int]] = []
    lowered = text.casefold()
    for word in head_words:
        needle = word.casefold().strip()
        if len(needle) < 4:
            continue
        start = lowered.find(needle)
        while start >= 0:
            spans.append((start, start + len(needle)))
            start = lowered.find(needle, start + 1)
    return tuple(spans)


def split_unknown(
    text: str, vocabulary: frozenset[str], *, minimum: int = 3
) -> tuple[tuple[int, int], ...]:
    """Spans of unknown tokens that split into two words the book knows.

    The fused token a tight-kern join produces (``ofthe``) is not a dictionary
    word and the vocabulary check already flags it; this says *why*, and it also
    catches the case that check misses — a fusion that happens to be spelled like
    another word. The halves must be vocabulary words of at least ``minimum``
    letters, so a lucky two-letter coincidence cannot flag a real word.

    It never corrects anything: the token is marked, not rewritten.
    """
    spans: list[tuple[int, int]] = []
    for match in WORD.finditer(text):
        token = match.group(0).casefold()
        if token in vocabulary:
            continue
        for cut in range(minimum, len(token) - minimum + 1):
            head, tail = token[:cut], token[cut:]
            if head in vocabulary and tail in vocabulary:
                spans.append((match.start(), match.end()))
                break
    return tuple(spans)


def system_dictionary(path: str = "/usr/share/dict/words") -> frozenset[str] | None:
    """A dictionary when the machine has one, case-folded. Optional by design."""

    words = Path(path)
    if not words.is_file():
        return None
    try:
        return frozenset(line.strip().casefold() for line in words.read_text(errors="ignore").splitlines() if line.strip())
    except OSError:
        return None
