"""Alignment: what each reader said, compared character by character.

Design 7.4, and the single place the comparison rule lives. Readings are aligned
in both directions after a fixed normalization - Unicode NFC, ligatures expanded,
whitespace runs collapsed to one space - and **nothing else is folded**: case,
punctuation, signs, decimals and accents are significant, so ``US debt: -1.5%``
is not ``us debt: 1.5%`` and must never be treated as agreement.

Every edit is reported on both sides, in both coordinate spaces, because an edit
is a fact about a pair of texts: the page's truth and the reader's text. An
insertion, a deletion and a substitution are all disagreements, and a word one
reading has and another does not is a disagreement rather than a non-event.

This file is the WP0 measurement's alignment, moved where the service can use it:
``packages/ocr/tools/ocr_synth/metrics.py`` imports these names rather than
keeping a second copy, so the Phase 0 numbers and the production path cannot
drift apart.
"""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

OmissionWindow = 2  # chars: an omission is a point, a mark near it counts

PUNCTUATION = set(string.punctuation)

# Ligatures are expanded before comparison, exactly as the alignment rule says
# (design §7.4). Everything else there — case, punctuation, signs, decimals,
# accents — stays significant, so nothing is lowercased or stripped here.
LIGATURES: dict[str, str] = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "ft",
    "\ufb06": "st",
}

WHITESPACE_RUN = re.compile(r"\s+")


def normalize(text: str) -> str:
    """NFC, ligatures expanded, whitespace runs collapsed to one space."""
    expanded = unicodedata.normalize("NFC", text)
    for ligature, plain in LIGATURES.items():
        expanded = expanded.replace(ligature, plain)
    return WHITESPACE_RUN.sub(" ", expanded).strip()


@dataclass(frozen=True)
class Edit:
    """One difference between truth and final text, on both sides."""

    kind: str
    truth_start: int
    truth_end: int
    final_start: int
    final_end: int
    truth_text: str
    final_text: str

    @property
    def final_span(self) -> tuple[int, int]:
        return (self.final_start, self.final_end)


def _classify_char(truth_char: str, final_char: str) -> str:
    if truth_char.isdigit() and final_char.isdigit():
        return "digit"
    if truth_char in PUNCTUATION or final_char in PUNCTUATION:
        return "punctuation"
    if truth_char.isspace() or final_char.isspace():
        return "whitespace"
    if truth_char.isalpha() and final_char.isalpha():
        return "substitution"
    return "other"


# Equal runs this short between two differences are re-aligned as one block.
# Without it, a two-character swap inside a word ("form"/"from") is reported as
# an insertion plus an omission, which is not what a reader did to the page.
REALIGN_WINDOW = 3
# At the word level the same rule applies, counted in words: a deletion and an
# insertion around a single unchanged word are one swapped pair, not two events.
WORD_REALIGN_WINDOW = 3
MASK = "\x00"


def _coalesce(opcodes: list[tuple], window: int = REALIGN_WINDOW) -> list[tuple]:
    """Merge differences separated only by a short equal run into one block."""
    merged: list[tuple] = []
    index = 0
    while index < len(opcodes):
        tag, i1, i2, j1, j2 = opcodes[index]
        if tag != "equal" or len(merged) == 0:
            merged.append(opcodes[index])
            index += 1
            continue
        following = opcodes[index + 1] if index + 1 < len(opcodes) else None
        if i2 - i1 <= window and following is not None and merged[-1][0] != "equal" and following[0] != "equal":
            previous = merged.pop()
            end = following
            merged.append(("replace", previous[1], end[2], previous[3], end[4]))
            index += 2
            continue
        merged.append(opcodes[index])
        index += 1
    return merged


def _raw_edits(truth: str, final: str) -> list[Edit]:
    matcher = SequenceMatcher(None, truth, final, autojunk=False)
    edits: list[Edit] = []
    for tag, i1, i2, j1, j2 in _coalesce(list(matcher.get_opcodes())):
        if tag == "equal":
            continue
        if tag == "replace":
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                edits.append(
                    Edit(
                        kind=_classify_char(truth[i1 + offset], final[j1 + offset]),
                        truth_start=i1 + offset,
                        truth_end=i1 + offset + 1,
                        final_start=j1 + offset,
                        final_end=j1 + offset + 1,
                        truth_text=truth[i1 + offset],
                        final_text=final[j1 + offset],
                    )
                )
            for offset in range(paired, i2 - i1):
                edits.append(
                    Edit("omission", i1 + offset, i1 + offset + 1, j1 + paired, j1 + paired, truth[i1 + offset], "")
                )
            for offset in range(paired, j2 - j1):
                edits.append(
                    Edit("insertion", i1 + paired, i1 + paired, j1 + offset, j1 + offset + 1, "", final[j1 + offset])
                )
        elif tag == "delete":
            edits.append(Edit("omission", i1, i2, j1, j1, truth[i1:i2], ""))
        else:
            edits.append(Edit("insertion", i1, i1, j1, j2, "", final[j1:j2]))
    return edits


def _spans(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    """Words and their spans, split on single spaces."""
    words: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    for raw in text.split(" "):
        start = index
        end = start + len(raw)
        index = end + 1
        if raw:
            words.append(raw)
            spans.append((start, end))
    return words, spans


def _word_order_edits(
    truth: str, final: str
) -> tuple[list[Edit], list[tuple[int, int]], list[tuple[int, int]]]:
    """Find replaced runs that are the same words in a different order.

    A transposition cannot be seen from characters, so it is looked for at the
    word level first; the character ranges it covers are then masked out of the
    character-level pass, or the same event would be counted twice.
    """
    truth_words, truth_spans = _spans(truth)
    final_words, final_spans = _spans(final)
    if not truth_words or not final_words:
        return [], [], []
    matcher = SequenceMatcher(None, truth_words, final_words, autojunk=False)
    orders: list[Edit] = []
    consumed_truth: list[tuple[int, int]] = []
    consumed_final: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in _coalesce(list(matcher.get_opcodes()), WORD_REALIGN_WINDOW):
        if tag != "replace":
            continue
        left = truth_words[i1:i2]
        right = final_words[j1:j2]
        if len(left) < 2 or len(left) != len(right) or sorted(left) != sorted(right) or left == right:
            continue
        truth_start, truth_end = truth_spans[i1][0], truth_spans[i2 - 1][1]
        final_start, final_end = final_spans[j1][0], final_spans[j2 - 1][1]
        orders.append(
            Edit(
                kind="order",
                truth_start=truth_start,
                truth_end=truth_end,
                final_start=final_start,
                final_end=final_end,
                truth_text=truth[truth_start:truth_end],
                final_text=final[final_start:final_end],
            )
        )
        consumed_truth.append((truth_start, truth_end))
        consumed_final.append((final_start, final_end))
    return orders, consumed_truth, consumed_final


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    out = list(text)
    for start, end in spans:
        for position in range(start, end):
            out[position] = MASK
    return "".join(out)


def classify_errors(truth: str, final: str) -> tuple[Edit, ...]:
    """Every difference between truth and final, with word order separated out.

    Character-level alignment cannot see a transposition as one event: it sees
    an omission and an insertion, or four substitutions. A replaced run that is
    the same words in another order is therefore reported as a single ``order``
    edit, which is the class the design asks to be measured separately.
    """
    orders, consumed_truth, consumed_final = _word_order_edits(truth, final)
    masked_truth = _mask(truth, consumed_truth)
    masked_final = _mask(final, consumed_final)
    edits = [
        edit
        for edit in _raw_edits(masked_truth, masked_final)
        if MASK not in edit.truth_text and MASK not in edit.final_text
    ]
    return tuple(sorted([*orders, *edits], key=lambda edit: (edit.final_start, edit.truth_start)))


Interval = tuple[int, int]


def overlaps(intervals: Iterable[Interval], span: Interval, window: int = OmissionWindow) -> bool:
    """Does any interval touch ``span``? Empty spans are points with a window."""
    start, end = span
    for left, right in intervals:
        if start == end:
            if left - window <= start <= right + window:
                return True
        elif left < end and start < right:
            return True
    return False
