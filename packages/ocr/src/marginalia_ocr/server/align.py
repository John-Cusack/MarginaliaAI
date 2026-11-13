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
    from collections.abc import Iterable, Mapping, Sequence

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


# --------------------------------------------------------------------------- #
# three readings, one sequence of agreed and disputed segments
# --------------------------------------------------------------------------- #

AGREED = "agreed"
DISPUTED = "disputed"

#: The order a spine is chosen in when the named primary produced nothing. Fixed,
#: not "whichever reader happens to be first in the dict": the fallback must be
#: the same on every page or the stored text's provenance changes with a dict.
PRIMARY_ORDER: tuple[str, ...] = ("tesseract", "vlm", "third")

#: A token for alignment: a run of non-space characters. Punctuation stays
#: attached, because §7.4 makes punctuation significant - "48.2%" and "482%"
#: must not look like the same token.
TOKEN = re.compile(r"\S+")


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Segment:
    """One run of the page: every reader the same, or a dispute to resolve.

    ``span`` and ``text`` are in the *primary* reading's coordinates and keep its
    original whitespace and line breaks, because that is the text the pipeline
    stores. Normalization is for comparing, never for storage: collapsing the
    whitespace would throw away the paragraph and line structure of the book.

    ``candidates`` is one entry per reader - ``(reader, text)`` - and an empty
    string is a candidate like any other: a reader that produced nothing at a
    place the others read something is a disagreement, not silence.
    """

    kind: str
    span: tuple[int, int]
    text: str
    candidates: tuple[tuple[str, str], ...] = ()

    @property
    def agreed(self) -> bool:
        return self.kind == AGREED


def tokens_of(text: str) -> tuple[Token, ...]:
    return tuple(Token(match.group(0), match.start(), match.end()) for match in TOKEN.finditer(text))


def _aligned_to_primary(
    primary_tokens: Sequence[Token], other_tokens: Sequence[Token]
) -> tuple[dict[int, tuple[int, int]], dict[int, tuple[int, int]]]:
    """Where the other reading's text sits, by the primary index it belongs to.

    Returns offsets into the *other* reading's own text, not its words: a
    candidate has to be what that reader actually produced, with its own spacing,
    or a candidate is a reconstruction rather than a reading. ``extra`` is the
    text it has where the primary has nothing, attributed to the primary index it
    follows, so a word one reader saw and nobody else did is a visible dispute.
    """
    matched: dict[int, tuple[int, int]] = {}
    extra: dict[int, tuple[int, int]] = {}
    matcher = SequenceMatcher(
        None, [token.text for token in primary_tokens], [token.text for token in other_tokens]
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                token = other_tokens[j1 + offset]
                matched[i1 + offset] = (token.start, token.end)
        elif tag == "insert":
            if j1 < j2:
                key = max(0, i1 - 1)
                span = (other_tokens[j1].start, other_tokens[j2 - 1].end)
                if key in extra:
                    span = (min(extra[key][0], span[0]), max(extra[key][1], span[1]))
                extra[key] = span
        elif tag == "replace":
            # Pair what can be paired, then keep the rest. A replace whose two
            # sides differ in length is both a substitution and an insertion, and
            # dropping the extra tokens is how a reader's words vanish: the
            # candidates must carry every word that reader produced.
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                token = other_tokens[j1 + offset]
                matched[i1 + offset] = (token.start, token.end)
            if j2 - j1 > paired:
                span = (other_tokens[j1 + paired].start, other_tokens[j2 - 1].end)
                key = i1 + paired - 1 if paired else max(0, i1 - 1)
                if key in extra:
                    span = (min(extra[key][0], span[0]), max(extra[key][1], span[1]))
                extra[key] = span
    return matched, extra


def align_readings(
    readings: Mapping[str, str], *, primary: str | None = None
) -> tuple[Segment, ...]:
    """Align three (or more) readings into agreed and disputed segments.

    Word granularity, and the alignment resynchronizes: a disagreement about one
    word is a segment of one word, and the words after it are aligned again rather
    than dragged along by the dispute - which is what keeps a single bad word from
    marking the rest of the page uncertain.
    """
    if not readings:
        return ()
    named = primary or next(iter(readings))
    if named not in readings:
        raise ValueError(f"primary reader {named!r} is not among the readings")
    # A blank primary must not swallow the other readers' text. The spine comes
    # from the next non-blank reader in a fixed order, and the caller is told that
    # the spine is not the primary's, so it can record why.
    primary, spine = spine_reader(readings, named)

    votes: list[dict[str, str]] = [{primary: token.text} for token in spine]
    where: dict[str, dict[int, tuple[int, int]]] = {primary: {i: (t.start, t.end) for i, t in enumerate(spine)}}
    extras: dict[str, dict[int, tuple[int, int]]] = {}
    for reader, text in readings.items():
        if reader == primary:
            continue
        matched, extra = _aligned_to_primary(spine, tokens_of(text))
        where[reader] = matched
        extras[reader] = extra
        for index, vote in enumerate(votes):
            vote[reader] = text[matched[index][0] : matched[index][1]] if index in matched else ""

    segments: list[Segment] = []
    index = 0
    while index < len(spine):
        agreed = _is_agreed(votes[index], readings) and not any(
            index in extras.get(reader, {}) for reader in readings if reader != primary
        )
        end = index
        while end + 1 < len(spine):
            next_agreed = _is_agreed(votes[end + 1], readings) and not any(
                end + 1 in extras.get(reader, {}) for reader in readings if reader != primary
            )
            if next_agreed != agreed:
                break
            end += 1
        span = (spine[index].start, spine[end].end)
        if agreed:
            segments.append(Segment(kind=AGREED, span=span, text=readings[primary][span[0] : span[1]]))
        else:
            candidates = tuple(
                (reader, _candidate_for(reader, readings, where, extras, index, end))
                for reader in readings
            )
            segments.append(
                Segment(
                    kind=DISPUTED,
                    span=span,
                    text=readings[primary][span[0] : span[1]],
                    candidates=candidates,
                )
            )
        index = end + 1
    return tuple(segments)


def spine_reader(
    readings: Mapping[str, str], named: str
) -> tuple[str, tuple[Token, ...]]:
    """Which reading the spine comes from, and its tokens.

    The named primary when it has text; otherwise the first non-blank reader in
    ``PRIMARY_ORDER``. When every reading is blank the named primary's empty spine
    is returned, and the caller has nothing to align - which is a gap, not a
    confirmed empty page.
    """
    if tokens_of(readings.get(named, "")):
        return named, tokens_of(readings[named])
    for candidate in PRIMARY_ORDER:
        if candidate in readings and tokens_of(readings[candidate]):
            return candidate, tokens_of(readings[candidate])
    for reader, text in readings.items():
        if tokens_of(text):
            return reader, tokens_of(text)
    return named, ()


def _is_agreed(vote: Mapping[str, str], readings: Mapping[str, str]) -> bool:
    """Every reader the same after §7.4's normalization, and none of them empty."""
    texts = [normalize(vote.get(reader, "")) for reader in readings]
    return bool(texts) and texts[0] != "" and len(set(texts)) == 1


def _candidate_for(
    reader: str,
    readings: Mapping[str, str],
    where: Mapping[str, Mapping[int, tuple[int, int]]],
    extras: Mapping[str, Mapping[int, tuple[int, int]]],
    first: int,
    last: int,
) -> str:
    """What this reader has for the disputed run, in its own words and spacing.

    Sliced out of the reader's own text rather than rebuilt from words: the
    candidate has to be something the reader produced, because the tie-break's
    answer is checked against it and the mark records it.
    """
    text = readings[reader]
    spans: list[tuple[int, int]] = []
    for index in range(first, last + 1):
        if index in where.get(reader, {}):
            spans.append(where[reader][index])
        if index in extras.get(reader, {}):
            spans.append(extras[reader][index])
    if not spans:
        return ""
    return text[min(span[0] for span in spans) : max(span[1] for span in spans)]
