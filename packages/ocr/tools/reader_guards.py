"""Guards on a reader's own output: a loop is a gap, never text.

The local VLM, asked to read a 1x region crop, returned 7,923 characters of one
sentence repeated — the classic degenerate loop of a vision-language model on an
image too small for it (design §7.2 readers). A reader that can do that cannot be
trusted to *report* its failure: the guard has to notice it downstream, on the
text itself, and turn it into a gap. A gap blocks the page, which is the design's
rule for ink no reader turned into text; text that is a loop is worse than no
text, because it is stored, embedded, and quotable.

Two signals, both cheap and both about shape rather than content:

* a short window of words repeated many times in a row;
* a reading far longer than the ink it claims to describe.

The second needs a capacity for the region. It is derived from the region's own
size and the type size the render declares, not from the truth: a region of
``w x h`` pixels at ``font_size`` holds about ``w / (0.55 * font_size)``
characters per line and ``h / (1.35 * font_size)`` lines, and a reading past a
generous multiple of that capacity is not a reading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A window this long, repeated this many times in a row, is a loop. The total
# count matters too: a loop whose period is not a multiple of the window never
# shows up as consecutive runs — "is not a detail: a list of revisions read",
# repeated 200 times, has a nine-word period and no two equal eight-word blocks
# in a row — while six verbatim eight-word blocks anywhere in one region's
# reading is already degenerate for prose.
REPEAT_WINDOW_WORDS = 8
REPEAT_RUN = 3
REPEAT_TOTAL = 6
# A loop's period is whatever the model settled on — the 1x VLM's was nine words,
# a repeated table row is five — so the density is measured over every window a
# short loop can have, not just one.
REPEAT_WINDOWS = tuple(range(3, 13))
REPEAT_COVERAGE = 0.5
# A reading this many times the region's capacity is not describing the region.
CAPACITY_MULTIPLE = 3.0
CHARS_PER_EM = 0.55
LINES_PER_EM = 1.35

WORD = re.compile(r"\S+")


@dataclass(frozen=True)
class Guarded:
    """A reader's text, or a gap where the text was not trustworthy."""

    text: str
    gap: bool
    reason: str = ""

    @property
    def chars(self) -> int:
        return len(self.text)


def region_capacity(box: tuple[float, float, float, float], font_size: float | None) -> int | None:
    """Roughly how many characters a region of this size can hold, or ``None``."""
    if not font_size or font_size <= 0:
        return None
    width, height = box[2] - box[0], box[3] - box[1]
    if width <= 0 or height <= 0:
        return None
    per_line = max(1.0, width / (CHARS_PER_EM * font_size))
    lines = max(1.0, height / (LINES_PER_EM * font_size))
    return int(per_line * lines)


def most_common_block(text: str, window: int = REPEAT_WINDOW_WORDS) -> int:
    """How often the most repeated ``window``-word block appears anywhere."""
    words = WORD.findall(text)
    if len(words) < window:
        return 0
    counts: dict[tuple[str, ...], int] = {}
    for index in range(len(words) - window + 1):
        block = tuple(words[index : index + window])
        counts[block] = counts.get(block, 0) + 1
    return max(counts.values(), default=0)


def repetition_density(text: str) -> tuple[int, int, float]:
    """The best ``(count, window, coverage)`` of any repeated block.

    Coverage is the fraction of the reading taken up by that block's repeats, so
    a refrain in prose scores low and a loop scores high, whatever its period.
    """
    words = WORD.findall(text)
    if len(words) < REPEAT_WINDOWS[0]:
        return (0, 0, 0.0)
    best = (0, 0, 0.0)
    for window in REPEAT_WINDOWS:
        if len(words) < window:
            break
        counts: dict[tuple[str, ...], int] = {}
        for index in range(len(words) - window + 1):
            block = tuple(words[index : index + window])
            counts[block] = counts.get(block, 0) + 1
        count = max(counts.values(), default=0)
        coverage = count * window / len(words)
        if coverage > best[2]:
            best = (count, window, coverage)
    return best


def longest_repeat_run(text: str, window: int = REPEAT_WINDOW_WORDS) -> int:
    """How many times in a row the most repeated ``window``-word block appears."""
    words = WORD.findall(text)
    if len(words) < window * 2:
        return 1
    best = 1
    index = 0
    while index + window <= len(words):
        block = words[index : index + window]
        repeats = 1
        cursor = index + window
        while cursor + window <= len(words) and words[cursor : cursor + window] == block:
            repeats += 1
            cursor += window
        best = max(best, repeats)
        index += 1
    return best


def guard_reading(
    text: str,
    *,
    box: tuple[float, float, float, float] | None = None,
    font_size: float | None = None,
    capacity_multiple: float = CAPACITY_MULTIPLE,
) -> Guarded:
    """Return the reading, or a gap when it loops or overshoots its region."""
    stripped = text.strip()
    if not stripped:
        return Guarded(text="", gap=True, reason="empty reading")
    run = longest_repeat_run(stripped)
    if run >= REPEAT_RUN:
        return Guarded(
            text="", gap=True, reason=f"repetition loop: {run} repeats of a {REPEAT_WINDOW_WORDS}-word block"
        )
    count, window, coverage = repetition_density(stripped)
    # A phrase can repeat in ordinary prose — a refrain, a table's stem words — so
    # the count alone is not enough: the repeated block has to be the *bulk* of
    # what the region claims to say. A loop is mostly itself.
    if count >= REPEAT_TOTAL and coverage >= REPEAT_COVERAGE:
        return Guarded(
            text="",
            gap=True,
            reason=f"repetition: a {window}-word block repeats {count} times ({coverage:.0%} of the reading)",
        )
    if box is not None:
        capacity = region_capacity(box, font_size)
        if capacity is not None and len(stripped) > capacity * capacity_multiple:
            return Guarded(
                text="",
                gap=True,
                reason=f"{len(stripped)} characters where the region holds about {capacity}",
            )
    return Guarded(text=stripped, gap=False)
