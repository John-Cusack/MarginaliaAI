"""Confirm, tie-break, mark, and the book's vocabulary (design 7.4).

The rule is deliberately harsh: a span is **confirmed** only when every reader
agrees after the alignment normalization *and* no check objects. There is no
2-of-3, because the errors that matter are the ones readers share - three
recognizers from the same family of mistakes agreeing on a wrong word is the
failure mode this whole design exists to prevent.

Everything that is not confirmed is marked, with its candidates and the reason,
permanently. A mark is not a failure of the pipeline; it is the pipeline saying
what it does not know, and the assessment is built from them.

The tie-break chain is the design's, in order: re-read the region from 3x pixels;
prefer a candidate that equals a *confirmed* occurrence elsewhere in the book;
let the VLM choose **among the candidates**. The last one cannot introduce text:
the chooser is an interface, its answer is checked against the candidate set, and
an answer that is not one of them is refused rather than stored.

Tie-break output never enters the vocabulary. The vocabulary is what the book has
*confirmed*, and a chosen candidate is precisely the case where we did not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .align import normalize
from .checks import WORD

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class Candidate:
    """One text a reader proposed for a region, and who proposed it."""

    text: str
    readers: tuple[str, ...] = ()
    source: str = "reader"


@dataclass(frozen=True)
class Mark:
    """A span the pipeline will not confirm, with what it was choosing between."""

    span: tuple[int, int]
    kind: str
    reason: str
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class Gap:
    """Ink nothing could turn into text: the page is not complete."""

    span: tuple[int, int]
    reason: str


@dataclass(frozen=True)
class Confirmation:
    text: str
    marks: tuple[Mark, ...]
    gaps: tuple[Gap, ...]
    confirmed_spans: tuple[tuple[int, int], ...]
    resolved_by: str | None = None

    @property
    def confirmed(self) -> bool:
        return not self.marks and not self.gaps


class VlmChooser(Protocol):
    """The tie-break's last step: pick one of these, or nothing."""

    def choose(self, region_id: str, candidates: Sequence[str]) -> str | None: ...


class Vocabulary:
    """The book's confirmed words, and nothing else.

    Only confirmed spans may be added, and a word has to be seen ``minimum``
    times before it counts as the book's own - one occurrence is as likely to be
    a misread as a word. There is deliberately no method that adds unconfirmed
    text: the tie-break's output is exactly the text the book did *not* confirm,
    and a vocabulary that swallowed it would then bless the same mistake on the
    next page.
    """

    def __init__(self, minimum: int = 3) -> None:
        self.minimum = minimum
        self._counts: dict[str, int] = {}
        self._confirmed_texts: list[str] = []

    def add_confirmed(self, text: str) -> None:
        self._confirmed_texts.append(text)
        for word in WORD.findall(text):
            self._counts[word] = self._counts.get(word, 0) + 1

    @property
    def words(self) -> frozenset[str]:
        return frozenset(
            word for word, count in self._counts.items() if count >= self.minimum
        )

    def contains(self, word: str) -> bool:
        """Is this word one the book has confirmed? Casefolded, exact otherwise."""
        return word in self.words

    @property
    def confirmed_texts(self) -> tuple[str, ...]:
        return tuple(self._confirmed_texts)

    @classmethod
    def from_texts(cls, texts: Iterable[str], *, minimum: int = 3) -> Vocabulary:
        vocabulary = cls(minimum=minimum)
        for text in texts:
            vocabulary.add_confirmed(text)
        return vocabulary


def candidates_from(readings: Mapping[str, str]) -> tuple[Candidate, ...]:
    """The distinct readings, in the order the readers were given, with their names.

    Empty readings are disagreements, not silence: a reader that produced nothing
    while another produced text means the span is not confirmed, and the empty
    answer is kept as a candidate so the mark says so.
    """
    grouped: dict[str, list[str]] = {}
    for reader, text in readings.items():
        grouped.setdefault(normalize(text), []).append(reader)
    return tuple(
        Candidate(text=text, readers=tuple(grouped[text])) for text in grouped
    )


def confirm_region(
    region_id: str,
    readings: Mapping[str, str],
    *,
    checks: Sequence[tuple[str, tuple[tuple[int, int], ...]]] = (),
    gapping: Iterable[str] = ("line_count", "chrome"),
    reread: Callable[[], str] | None = None,
    book: Vocabulary | None = None,
    chooser: VlmChooser | None = None,
) -> Confirmation:
    """Confirm this region, or mark it and say why.

    ``readings`` is per reader. ``checks`` is what the independent checks found in
    the *candidate* text, already in its coordinates. ``reread`` returns a fresh
    reading of the region if one can be taken (3x pixels, design 7.4) - it is the
    first tie-break, and it is a callable so the chain is testable without a
    browser or a GPU.
    """
    candidates = candidates_from(readings)
    texts = [candidate.text for candidate in candidates]
    unanimous = len(candidates) == 1 and candidates[0].text.strip() != ""

    gapping = tuple(gapping)
    if unanimous:
        text = candidates[0].text
        failed = sorted(name for name, spans in checks if spans)
        if not failed:
            return Confirmation(text=text, marks=(), gaps=(), confirmed_spans=((0, len(text)),))
        # The readers agree and a check objects: agreement is not correctness.
        # A check that says the page is incomplete gaps it; the rest mark it.
        if any(name in gapping for name in failed):
            gap = Gap(
                span=(0, len(text)),
                reason="the readers agree but " + ", ".join(failed) + " says the page is not complete",
            )
            return Confirmation(text=text, marks=(), gaps=(gap,), confirmed_spans=())
        mark = Mark(
            span=(0, len(text)),
            kind="check",
            reason="the readers agree but " + ", ".join(failed) + " objects",
            candidates=candidates,
        )
        return Confirmation(text=text, marks=(mark,), gaps=(), confirmed_spans=())

    # A disagreement. The chain runs in the design's order and each step is
    # recorded, because "the VLM picked this" and "the book already confirmed
    # this elsewhere" are different claims about the same text.
    chosen: Candidate | None = None
    resolved_by: str | None = None

    if reread is not None and all(text.strip() for text in texts):
        fresh = Candidate(text=normalize(reread()), source="reread")
        if fresh.text in texts:
            chosen, resolved_by = Candidate(fresh.text, source="reread"), "reread"

    if chosen is None and book is not None:
        # A candidate the book has already confirmed elsewhere is the strongest
        # evidence available: it is the book's own word, not a reader's guess.
        in_book: list[Candidate] = []
        for candidate in candidates:
            words = WORD.findall(candidate.text)
            if words and candidate.text.strip() and all(book.contains(word) for word in words):
                in_book.append(Candidate(candidate.text, candidate.readers, source="book"))
        if len(in_book) == 1:
            chosen, resolved_by = in_book[0], "book"

    if chosen is None and chooser is not None and all(text.strip() for text in texts):
        answer = chooser.choose(region_id, texts)
        if answer is not None and normalize(answer) in texts:
            chosen, resolved_by = Candidate(normalize(answer), source="vlm"), "vlm"

    if chosen is None:
        mark = Mark(
            span=(0, max((len(text) for text in texts), default=0)),
            kind="disagreement",
            reason="the readers disagree and no tie-break applies",
            candidates=candidates,
        )
        return Confirmation(text=candidates[0].text if candidates else "", marks=(mark,), gaps=(), confirmed_spans=())

    # Adopted, and still marked: a tie-break is a decision, not a confirmation.
    mark = Mark(
        span=(0, len(chosen.text)),
        kind=f"tiebreak:{resolved_by}",
        reason=f"resolved by {resolved_by}; not confirmed, and not added to the vocabulary",
        candidates=candidates,
    )
    return Confirmation(
        text=chosen.text, marks=(mark,), gaps=(), confirmed_spans=(), resolved_by=resolved_by
    )


def vocabulary_from_confirmations(confirmations: Iterable[Confirmation], *, minimum: int = 3) -> Vocabulary:
    """The book's vocabulary, from confirmed spans only.

    Everything else - a tie-break, or a mark with candidates - contributes
    nothing, which is what keeps a tied break from teaching the next page that the
    mistake was right.
    """
    vocabulary = Vocabulary(minimum=minimum)
    for confirmation in confirmations:
        if confirmation.confirmed_spans:
            vocabulary.add_confirmed(confirmation.text)
    return vocabulary


def check_spans_for(text: str, *, vocabulary: frozenset[str], dictionary: frozenset[str] | None = None) -> tuple[tuple[str, tuple[tuple[int, int], ...]], ...]:
    """The check set as `confirm_region` wants it: names and spans in this text."""
    from . import checks as check_module

    return (
        ("vocabulary", check_module.unmatched_words(text, vocabulary, dictionary)),
        ("dashed", check_module.dashed(text)),
        ("digit", check_module.implausible_numbers(text)),
        ("chrome", check_module.chrome_leaks(text)),
        ("split", check_module.split_unknown(text, vocabulary)),
    )
