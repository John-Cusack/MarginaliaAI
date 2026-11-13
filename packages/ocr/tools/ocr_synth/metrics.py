"""Scoring: character errors by class, marks and gaps, and audit sensitivity.

Nothing here reads a page. It takes a page's truth text, whatever the pipeline
(or a single reader) produced for it, and the marks and gaps it reported, and
says what the difference is — so that the Phase 0 numbers, the per-book audit
and the release rule are all computed by one piece of arithmetic instead of
three.

Two coordinates are in play and are never mixed:

* the page truth and a reader's text are compared as strings, and every edit is
  reported on both sides;
* marks and gaps are intervals in the *final* text, which is the text the
  assessment is attached to, again per §11.1.

The audit bound is Clopper-Pearson, one-sided, on the planted-error set (§9.3),
and the per-book release rule is the rule of three divided by that sensitivity
(§9.4) — with the sample size and sensitivity as explicit arguments, because a
bound whose inputs are implicit is a bound nobody can check.
"""

from __future__ import annotations

import math
import re
import string
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .seeded_errors import PlantedError, SeededText

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


@dataclass(frozen=True)
class PageScore:
    truth_chars: int
    final_chars: int
    edits: tuple[Edit, ...] = ()
    unmarked: tuple[Edit, ...] = ()
    marks: tuple[Interval, ...] = ()
    gaps: tuple[Interval, ...] = ()

    @property
    def errors(self) -> int:
        return len(self.edits)

    @property
    def unmarked_errors(self) -> int:
        return len(self.unmarked)

    @property
    def marked_errors(self) -> int:
        return self.errors - self.unmarked_errors

    @property
    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for edit in self.edits:
            counts[edit.kind] = counts.get(edit.kind, 0) + 1
        return counts

    @property
    def marks_on_correct_text(self) -> int:
        """Marks that touch no edit: the cost side of marking (design §9)."""
        spans = [edit.final_span for edit in self.edits]
        return sum(1 for mark in self.marks if not overlaps(spans, mark))

    def has_unmarked(self) -> bool:
        return bool(self.unmarked)


def score_page(
    truth: str,
    final: str,
    marks: Sequence[Interval] = (),
    gaps: Sequence[Interval] = (),
    *,
    normalized: bool = False,
) -> PageScore:
    """Score one page: what differs, and how much of it carries a mark.

    ``normalized`` compares the two texts after :func:`normalize`, which is the
    frame the alignment rule works in; marks and gaps are then in that same
    frame, so a caller that has raw offsets must map them itself.
    """
    left, right = (normalize(truth), normalize(final)) if normalized else (truth, final)
    edits = classify_errors(left, right)
    mark_list = tuple(sorted(marks))
    unmarked = tuple(edit for edit in edits if not overlaps(mark_list, edit.final_span))
    return PageScore(
        truth_chars=len(left),
        final_chars=len(right),
        edits=edits,
        unmarked=unmarked,
        marks=mark_list,
        gaps=tuple(sorted(gaps)),
    )


def aggregate(scores: Sequence[PageScore]) -> dict[str, object]:
    """The per-run table: error classes, marks, gaps, and errors per bad page."""
    by_kind: dict[str, int] = {}
    for score in scores:
        for kind, count in score.by_kind.items():
            by_kind[kind] = by_kind.get(kind, 0) + count
    pages = len(scores)
    bad = [score for score in scores if score.unmarked_errors]
    return {
        "pages": pages,
        "pages_with_unmarked": len(bad),
        "unmarked_errors": sum(score.unmarked_errors for score in scores),
        "marked_errors": sum(score.marked_errors for score in scores),
        "marks": sum(len(score.marks) for score in scores),
        "marks_on_correct_text": sum(score.marks_on_correct_text for score in scores),
        "gaps": sum(len(score.gaps) for score in scores),
        "errors_per_bad_page": (
            sum(score.unmarked_errors for score in bad) / len(bad) if bad else 0.0
        ),
        "by_kind": by_kind,
    }


# --------------------------------------------------------------------------- #
# audit sensitivity and the release rule
# --------------------------------------------------------------------------- #


def _log_binom(k: int, n: int, p: float) -> float:
    return (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        + k * math.log(p)
        + (n - k) * math.log1p(-p)
    )


def tail_probability(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), computed in log space."""
    if k <= 0:
        return 1.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    return sum(math.exp(_log_binom(index, n, p)) for index in range(k, n + 1))


def clopper_pearson_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided 95% lower confidence bound on a binomial proportion.

    ``k = 0`` gives 0: with no successes observed, no positive lower bound is
    justified. That is the whole reason Phase 0 must establish the audit
    reader's sensitivity on the seeded set before any book can be released.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= k <= n:
        raise ValueError("k must be between 0 and n")
    if k == 0:
        return 0.0
    low, high = 0.0, 1.0
    for _ in range(100):
        middle = (low + high) / 2
        # The upper tail is increasing in p, so a tail below alpha puts the root
        # above the middle: the bound is where the tail equals alpha.
        if tail_probability(k, n, middle) < alpha:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def rule_of_three(n: int) -> float:
    """Upper 95% bound on a rate with zero observed events in ``n`` draws."""
    if n <= 0:
        raise ValueError("n must be positive")
    return 3.0 / n


def clopper_pearson_upper(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided 95% upper confidence bound on a binomial proportion.

    The dual of the lower bound: the upper bound for ``k`` successes is one
    minus the lower bound for ``n - k`` failures. The per-page unmarked-error
    rate of a measured run is quoted with this, which is the same shape of claim
    the per-book release rule makes.
    """
    return 1.0 - clopper_pearson_lower(n - k, n, alpha)


def release_bound(pages: int, sensitivity_lower: float) -> float:
    """Upper bound on the fraction of pages with an unmarked error (§9.4)."""
    if pages <= 0:
        raise ValueError("pages must be positive")
    if sensitivity_lower <= 0:
        return math.inf
    return rule_of_three(pages) / sensitivity_lower


def release_allowed(pages: int, sensitivity_lower: float, detections: int) -> bool:
    """The predeclared release rule: no detections, and the bound under 0.10."""
    return detections == 0 and release_bound(pages, sensitivity_lower) <= 0.10


@dataclass(frozen=True)
class Detection:
    """One difference an audit reader reports, in final-text coordinates."""

    final_start: int
    final_end: int
    snippet: str = ""
    confidence: float | None = None
    # What the auditor says is there, as it read it. A strict detection needs it:
    # a difference that merely overlaps the planted span is satisfied by an
    # auditor that reports everything, so the reading itself is the measurement.
    reading: str = ""

    @property
    def span(self) -> Interval:
        return (self.final_start, self.final_end)


@dataclass(frozen=True)
class SensitivityResult:
    """How much of the planted error set a reader reported, with its bound."""

    reader: str
    planted: int
    detected: int
    misses: tuple[PlantedError, ...] = ()
    alpha: float = 0.05
    pages: int = 0
    by_kind: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def sensitivity(self) -> float:
        return self.detected / self.planted if self.planted else 0.0

    @property
    def lower(self) -> float:
        """The one-sided 95% lower bound, or 0 when nothing was measured.

        An empty set has no bound to report; 0 is the honest value and it makes
        the release rule refuse, which is the right direction for a measurement
        that failed to happen.
        """
        if self.planted <= 0:
            return 0.0
        return clopper_pearson_lower(self.detected, self.planted, self.alpha)


def audit_sensitivity(
    reader: str,
    seeded: Sequence[SeededText],
    detect: Callable[[SeededText], Sequence[Detection]],
    alpha: float = 0.05,
    strict: bool = False,
) -> SensitivityResult:
    """Measure one audit reader's sensitivity on a predeclared seeded set.

    ``strict`` is the definition design 7.4 implies: a detection is a difference
    at the span that *reads the truth there*, after the same normalization the
    aligner uses (NFC, ligatures expanded, whitespace collapsed).
    """
    planted = 0
    detected = 0
    misses: list[PlantedError] = []
    by_kind: dict[str, list[int]] = {}
    for page in seeded:
        detections = list(detect(page))
        spans = [detection.span for detection in detections]
        for error in page.errors:
            planted += 1
            hit = overlaps(spans, error.final_span)
            if strict and hit:
                # Design 7.4: a difference counts as finding the error only if the
                # auditor reads the truth there, compared after the aligner's own
                # normalization. Overlap alone is satisfied by reporting the whole
                # page, which is how a pathological auditor scored 1.0.
                wanted = normalize(error.truth_text)
                hit = any(
                    overlaps([detection.span], error.final_span)
                    and detection.reading
                    and normalize(detection.reading) == wanted
                    for detection in detections
                )
            detected += 1 if hit else 0
            if not hit:
                misses.append(error)
            seen, caught = by_kind.setdefault(error.kind, [0, 0])
            by_kind[error.kind] = [seen + 1, caught + (1 if hit else 0)]
    return SensitivityResult(
        reader=reader,
        planted=planted,
        detected=detected,
        misses=tuple(misses),
        alpha=alpha,
        pages=len(seeded),
        by_kind={kind: (seen, caught) for kind, (seen, caught) in by_kind.items()},
    )
