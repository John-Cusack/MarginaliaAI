"""The independent checks that run beside the readers (WP2, as a measurement stub).

A three-reader confirm step stores a span when the readers agree, so the errors
that matter are the ones agreement cannot catch *and* the checks cannot catch
either. Measuring an auditor's sensitivity over errors the checks would have
marked anyway measures the wrong population: those spans are marked in the
production pipeline, and the gate is about unmarked ones.

WP2 does not exist yet, so this is the smallest honest stand-in for the check set
the design calls for — a **book-vocabulary check**. A word is unmatched when it
appears in the page's text but not in the book's own vocabulary (the words the
book uses at least ``minimum`` times) and not in a dictionary when one is
supplied. That is exactly the shape of a check that catches a degraded-ink
confusion (``debr`` for ``debt``) or a case change (``MARGIN`` for ``Margin``)
without the readers' help.

What it does not model: hyphenation, hyphen/ndash confusion, running-head
removal, digit plausibility, or anything using syntax. Those arrive with WP2, and
`s_L` is only as good as this stand-in until they do.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

WORD = re.compile(r"[A-Za-z][A-Za-z'\u2019-]*")


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
    from pathlib import Path

    words = Path(path)
    if not words.is_file():
        return None
    try:
        return frozenset(line.strip().casefold() for line in words.read_text(errors="ignore").splitlines() if line.strip())
    except OSError:
        return None
