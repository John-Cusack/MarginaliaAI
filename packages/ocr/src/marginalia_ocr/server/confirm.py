"""Confirm, tie-break, mark, and the book's vocabulary (design 7.4).

The rule is deliberately harsh: a span is **confirmed** only when every reader
agrees after the alignment normalization *and* no check objects. There is no
2-of-3, because the errors that matter are the ones readers share - three
recognizers from the same family of mistakes agreeing on a wrong word is the
failure mode this whole design exists to prevent.

Everything that is not confirmed is marked, with its candidates and the reason,
permanently. A mark is not a failure of the pipeline; it is the pipeline saying
what it does not know, and the assessment is built from them.

The contract is **per word**, not per region: a 300-word region with one disputed
word is one mark of one word, with the other 299 confirmed. So the work is done on
the segments `align.align_readings` returns - agreed runs are confirmed (unless a
check flags a span inside them, and then only that span is marked), and a disputed
run is resolved on its own, from its own candidates.

The tie-break chain is the design's, in order: re-read the region from 3x pixels
(production is already 3x, so this means re-running the readers on the same stored
pixels - a 2x screen goes into the recapture queue instead); prefer a candidate
that equals a *confirmed* occurrence elsewhere in the book, and only when exactly
one candidate qualifies; let the VLM choose **among the candidates**. The last one
cannot introduce text: the chooser is an interface, its answer is checked against
the candidate set, and an answer that is not one of them is refused rather than
stored.

**Storage keeps the primary reading's own whitespace and line breaks.** The
normalization of 7.4 is for comparing and never for storing: collapsing the
whitespace would throw away the paragraph and line structure of the book. The
primary is the VLM (the best single-reader baseline Phase 0 measured), and where a
disputed segment has no tie-break its text is the primary's - which the mark says,
so the stored word is never silently one reader's guess among three.

Tie-break output never enters the vocabulary. The vocabulary is what the book has
*confirmed*, and a chosen candidate is precisely the case where we did not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .align import align_readings, normalize
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
        """Is this word one the book has confirmed? Exact, because §7.4 says so.

        Case is significant: "Margin" and "margin" are different words on the page
        and in the book, and a vocabulary that folded them would bless a case
        error as a word the book uses.
        """
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


def candidates_of(segment) -> tuple[Candidate, ...]:
    """A disputed segment's candidates as `Candidate`s, in reader order."""
    return tuple(Candidate(text=text, readers=(reader,)) for reader, text in segment.candidates)


def confirm_readings(
    readings: Mapping[str, str],
    *,
    primary: str = "vlm",
    checks: Sequence[tuple[str, tuple[tuple[int, int], ...]]] = (),
    gapping: Iterable[str] = ("line_count", "chrome"),
    reread: Callable[[object], str] | None = None,
    book: Vocabulary | None = None,
    chooser: VlmChooser | None = None,
) -> Confirmation:
    """Confirm a page's readings segment by segment, keeping the primary's layout.

    ``checks`` are spans in the *primary* reading's coordinates, because that is
    the text the checks were run on. ``reread`` is called per disputed segment.
    """
    if not readings:
        return Confirmation(text="", marks=(), gaps=(), confirmed_spans=())
    primary = primary if primary in readings else next(iter(readings))
    gapping = tuple(gapping)
    base = readings[primary]
    segments = align_readings(readings, primary=primary)

    # Every check span, mapped from the primary's coordinates into the stored text
    # as the stored text is built - a tie-break can change a segment's length, and
    # a mark that drifts off its own span is worse than no mark.
    check_hits: list[tuple[str, tuple[int, int]]] = [
        (name, span) for name, spans in checks for span in spans
    ]
    # A check that means "this page is not the page" gaps it whole: the page is
    # re-taken, so nothing on it is confirmed, however well the readers agreed.
    page_gapped = any(name in gapping for name, _span in check_hits)

    out: list[str] = []
    marks: list[Mark] = []
    gaps: list[Gap] = []
    confirmed: list[tuple[int, int]] = []
    cursor = 0
    length = 0  # the stored text's length so far, kept as we go

    for segment in segments:
        inter = base[cursor : segment.span[0]]
        out.append(inter)
        length += len(inter)
        start = length

        hits = [
            (name, span)
            for name, span in check_hits
            if span[0] < segment.span[1] and segment.span[0] < span[1]
        ]
        if segment.agreed and not hits:
            out.append(segment.text)
            length += len(segment.text)
            if not page_gapped:
                confirmed.append((start, length))
            cursor = segment.span[1]
            continue

        if segment.agreed:
            out.append(segment.text)
            names = sorted({name for name, _span in hits})
            # The hit marks its own span; everything else in the segment is still
            # confirmed. Marking the whole segment would turn one flagged word
            # into a page of uncertainty - the defect this rewrite exists to fix.
            if not page_gapped:
                # Only the spans no check touched are confirmed. A gapping check
                # means the page is re-taken, so none of it is confirmed at all.
                cut = length
                for span in sorted((span for _name, span in hits)):
                    start_of_hit = length + (span[0] - segment.span[0])
                    if start_of_hit > cut:
                        confirmed.append((cut, start_of_hit))
                    cut = max(cut, length + (span[1] - segment.span[0]))
                if cut < length + len(segment.text):
                    confirmed.append((cut, length + len(segment.text)))
            length += len(segment.text)
            for name, span in hits:
                width = span[1] - span[0]
                offset = start + (span[0] - segment.span[0])
                mark_span = (offset, offset + max(width, 1))
                if name in gapping:
                    gaps.append(
                        Gap(span=mark_span, reason=f"{name} says the page is not complete")
                    )
                else:
                    marks.append(
                        Mark(
                            span=mark_span,
                            kind="check",
                            reason="the readers agree but " + ", ".join(names) + " objects",
                            candidates=(Candidate(text=segment.text, source="agreed"),),
                        )
                    )
            cursor = segment.span[1]
            continue

        # A dispute, resolved on its own candidates.
        candidates = candidates_of(segment)
        texts = [candidate.text for candidate in candidates]
        chosen: str | None = None
        resolved_by: str | None = None

        if reread is not None and all(text.strip() for text in texts):
            fresh = normalize(reread(segment))
            match = next((text for text in texts if normalize(text) == fresh), None)
            if match is not None:
                chosen, resolved_by = match, "reread"

        if chosen is None and book is not None:
            # Distinct texts: two readers proposing the same qualifying word is
            # one candidate, not two, and counting them separately would make the
            # "exactly one" rule fail whenever two readers agreed with the book.
            qualifying = sorted(
                {
                    text
                    for text in texts
                    if text.strip()
                    and (words := WORD.findall(text))
                    and all(book.contains(word) for word in words)
                }
            )
            if len(qualifying) == 1:
                chosen, resolved_by = qualifying[0], "book"

        if chosen is None and chooser is not None and all(text.strip() for text in texts):
            answer = chooser.choose(segment_key(segment), texts)
            if answer is not None:
                match = next((text for text in texts if normalize(text) == normalize(answer)), None)
                if match is not None:
                    chosen, resolved_by = match, "vlm"

        if chosen is None:
            # No tie-break applies. The stored word is the primary's - the named
            # single reader Phase 0 measured best - and the mark says exactly that,
            # so it is never read as a decision the pipeline made on evidence.
            out.append(segment.text)
            length += len(segment.text)
            marks.append(
                Mark(
                    span=(start, length),
                    kind="disagreement",
                    reason=(
                        "the readers disagree and no tie-break applies; "
                        f"the stored text is the {primary} reader's, not confirmed"
                    ),
                    candidates=candidates,
                )
            )
        else:
            out.append(chosen)
            length += len(chosen)
            marks.append(
                Mark(
                    span=(start, length),
                    kind=f"tiebreak:{resolved_by}",
                    reason=f"resolved by {resolved_by}; not confirmed, and not added to the vocabulary",
                    candidates=candidates,
                )
            )
        cursor = segment.span[1]

    out.append(base[cursor:])
    return Confirmation(
        text="".join(out),
        marks=tuple(marks),
        gaps=tuple(gaps),
        confirmed_spans=tuple(confirmed),
    )


def segment_key(segment) -> str:
    """A stable name for a disputed segment, so the chooser can say where it looks."""
    return f"{segment.span[0]}-{segment.span[1]}"


def vocabulary_from_confirmations(confirmations: Iterable[Confirmation], *, minimum: int = 3) -> Vocabulary:
    """The book's vocabulary, from confirmed spans only.

    Everything else - a tie-break, or a mark with candidates - contributes
    nothing, which is what keeps a tied break from teaching the next page that the
    mistake was right.
    """
    vocabulary = Vocabulary(minimum=minimum)
    for confirmation in confirmations:
        # The spans, not the text: a confirmation's stored text also carries its
        # disputed segments, which are exactly the words the book did not confirm.
        for start, end in confirmation.confirmed_spans:
            vocabulary.add_confirmed(confirmation.text[start:end])
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
