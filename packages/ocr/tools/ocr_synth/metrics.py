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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .seeded_errors import PlantedError, SeededText

# The alignment is the service's (design 7.4) and lives in the dist: one copy,
# imported here rather than duplicated, so the Phase 0 numbers and the production
# path cannot disagree about what a difference is.
from marginalia_ocr.server.align import (  # noqa: F401
    LIGATURES,
    MASK,
    Edit,
    Interval,
    OmissionWindow,
    classify_errors,
    normalize,
    overlaps,
)


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
