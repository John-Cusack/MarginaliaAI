"""The structured importer: lay already-structured units out as canonical text.

A *unit* is the smallest citable piece a source has: a verse, an ayah, a
numbered paragraph, a catechism answer, a page of an unversified printing.
Whatever the source format — verse JSON, OSIS, a numbered confession — its
reader hands over units verbatim (original spelling, punctuation and
supplied-word markers untouched), and this module decides only where they sit,
the same way for every source:

- **The canonical text** is the units joined by a blank line, each prefixed by
  its printed label and a tab (``"7\\tAnd it came to pass…"``). The label is
  layout, not source: a unit's node spans its text alone, so a quotation of the
  verse resolves to the verse.
- **The tree** is a root for the document (a chapter, a surah, a sermon) and
  one node per unit, carrying the unit's reference.
- **The windows** that get embedded are whole units grouped up to a token
  budget, never splitting a unit unless it alone exceeds the embedder's
  ceiling, and never crossing the document.

The blank line is what makes the text self-describing: `UnitWindowChunker`
(core's `unit_window`) re-derives the same windows from the canonical text
alone, which is what lets `reindex chunks` rebuild these documents without the
reader. A unit whose own
text contains a blank line would break that, so it is refused — split it into
paragraphs instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from research_engine_sdk.chunking import (
    ABSOLUTE_MAX_TOKENS,
    approx_tokens,
    cap_spans,
    trim_span,
)
from research_engine_sdk.types import MarkDraft, NodeDraft, PassageDraft

SEP = "\n\n"
CHUNKER_ID = "unit_window"
CHUNKER_VERSION = "1"
DEFAULT_WINDOW_TOKENS = 350


@dataclass(frozen=True)
class UnitMark:
    """A range inside one unit's text — a translator's supplied words, say."""

    kind: str
    start: int
    end: int
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Unit:
    """One citable piece of a source, verbatim.

    ``ref`` is the canonical reference (``"1 Nephi 3:7"``, ``"WCF 31.3"``,
    ``"JD 1:50"``); ``label`` is what the source prints before it, possibly
    nothing. ``locator`` holds the structured parts of the reference.
    """

    ref: str
    text: str
    label: str = ""
    kind: str = "verse"
    locator: dict[str, Any] = field(default_factory=dict)
    marks: tuple[UnitMark, ...] = ()
    #: Zero-width marks just before this unit — a page break, typically.
    breaks_before: tuple[UnitMark, ...] = ()


@dataclass(frozen=True)
class UnitDocument:
    """One stored document: a chapter, a surah, a sermon, a volume's page run."""

    title: str
    source: str
    units: tuple[Unit, ...]
    language: str = "en"
    document_type: str = "versified_text"
    #: What the document node is called (`chapter`, `surah`, `discourse`).
    node_type: str = "chapter"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Layout:
    full_text: str
    nodes: list[NodeDraft]
    passages: list[PassageDraft]
    marks: list[MarkDraft]
    #: `(char_start, char_end)` of each unit's text, in unit order.
    unit_spans: list[tuple[int, int]]


class LayoutError(ValueError):
    """A unit cannot be laid out without breaking the canonical text's shape."""


def layout(document: UnitDocument, *, window_tokens: int = DEFAULT_WINDOW_TOKENS) -> Layout:
    if not document.units:
        raise LayoutError(f"{document.title}: a document needs at least one unit")

    pieces: list[str] = []
    unit_spans: list[tuple[int, int]] = []
    marks: list[MarkDraft] = []
    cursor = 0
    for index, unit in enumerate(document.units):
        _check(document, unit)
        if index:
            pieces.append(SEP)
            cursor += len(SEP)
        for mark in unit.breaks_before:
            marks.append(MarkDraft(kind=mark.kind, char_start=cursor, char_end=cursor,
                                   attrs=mark.attrs))
        prefix = f"{unit.label}\t" if unit.label else ""
        pieces.append(prefix + unit.text)
        start = cursor + len(prefix)
        end = start + len(unit.text)
        unit_spans.append((start, end))
        for mark in unit.marks:
            if not 0 <= mark.start <= mark.end <= len(unit.text):
                raise LayoutError(
                    f"{unit.ref}: {mark.kind} mark [{mark.start}, {mark.end}) is "
                    f"outside the unit's {len(unit.text)} characters"
                )
            marks.append(MarkDraft(kind=mark.kind, char_start=start + mark.start,
                                   char_end=start + mark.end, attrs=mark.attrs))
        cursor = end
    full_text = "".join(pieces)

    nodes = [
        NodeDraft(
            path="root", parent_path=None, depth=0, position=0,
            node_type=document.node_type, title=document.title,
            char_start=0, char_end=len(full_text), metadata=dict(document.metadata),
        )
    ]
    for position, (unit, (start, end)) in enumerate(
        zip(document.units, unit_spans, strict=True)
    ):
        nodes.append(
            NodeDraft(
                path=f"root.n{position}", parent_path="root", depth=1,
                position=position, node_type=unit.kind, title=unit.ref,
                char_start=start, char_end=end,
                metadata={"ref": unit.ref, **unit.locator},
            )
        )

    windows = window_spans(full_text, window_tokens=window_tokens)
    passages = [
        _passage(full_text, position, start, end, _locator(document, unit_spans, start, end))
        for position, (start, end) in enumerate(windows)
    ]
    return Layout(full_text, nodes, passages, marks, unit_spans)


def window_spans(
    text: str, *, window_tokens: int = DEFAULT_WINDOW_TOKENS
) -> list[tuple[int, int]]:
    """Group the text's blank-line-separated units into embedding windows."""
    units = _units(text)
    windows: list[tuple[int, int]] = []
    start = end = None
    for unit_start, unit_end in units:
        if start is None:
            start, end = unit_start, unit_end
            continue
        if approx_tokens(text[start:unit_end]) > window_tokens:
            windows.append((start, end))
            start = unit_start
        end = unit_end
    if start is not None:
        windows.append((start, end))
    return cap_spans(text, windows, ABSOLUTE_MAX_TOKENS)


class UnitWindowChunker:
    """Re-derives a laid-out document's windows from its canonical text alone.

    Core registers it as `unit_window`, so that `reindex chunks` can rebuild
    these documents: it looks a passage's chunker up by id and hands it
    nothing but the stored text.
    """

    id = CHUNKER_ID
    consumes = "text"
    version = CHUNKER_VERSION
    max_passage_tokens: int | None = ABSOLUTE_MAX_TOKENS

    def __init__(self, window_tokens: int = DEFAULT_WINDOW_TOKENS) -> None:
        self._window_tokens = window_tokens

    async def chunk(self, text: str, metadata: dict | None = None) -> list[PassageDraft]:
        if not text.strip():
            return []
        return [
            _passage(text, position, start, end, {})
            for position, (start, end) in enumerate(
                window_spans(text, window_tokens=self._window_tokens)
            )
        ]


def _units(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        cut = text.find(SEP, start)
        end = len(text) if cut < 0 else cut
        s, e = trim_span(text, start, end)
        if e > s:
            spans.append((s, e))
        if cut < 0:
            return spans
        start = cut + len(SEP)


def _check(document: UnitDocument, unit: Unit) -> None:
    where = f"{document.title}, {unit.ref}"
    if not unit.text.strip():
        raise LayoutError(f"{where}: the unit has no text")
    if SEP in unit.text:
        raise LayoutError(
            f"{where}: the unit's text contains a blank line, which would split it "
            f"in two on reindex. Split it into paragraph units instead."
        )
    if "\t" in unit.label or "\n" in unit.label:
        raise LayoutError(f"{where}: a label cannot contain a tab or newline")
    if unit.text != unit.text.strip():
        raise LayoutError(
            f"{where}: leading or trailing whitespace would fall outside the "
            f"unit's window; strip it in the parser, where the source is known"
        )


def _passage(
    text: str, position: int, start: int, end: int, locator: dict[str, Any]
) -> PassageDraft:
    body = text[start:end]
    return PassageDraft(
        position=position, char_start=start, char_end=end, text=body,
        token_count=approx_tokens(body), chunker=CHUNKER_ID,
        chunker_version=CHUNKER_VERSION, locator=locator,
    )


def _locator(
    document: UnitDocument, unit_spans: list[tuple[int, int]], start: int, end: int
) -> dict[str, Any]:
    covered = [
        unit for unit, (s, e) in zip(document.units, unit_spans, strict=True)
        if s < end and e > start
    ]
    if not covered:
        return {}
    first, last = covered[0], covered[-1]
    ref = first.ref if first is last else f"{first.ref} – {last.ref}"
    return {"ref": ref, "start": first.ref, "end": last.ref}
