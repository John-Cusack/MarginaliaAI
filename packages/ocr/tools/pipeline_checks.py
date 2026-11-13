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
