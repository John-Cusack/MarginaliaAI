"""Wikisource transcriptions of public-domain printings, read from the page.

Wikisource's robots.txt keeps crawlers off `/w/` (its API included), so the
reader fetches the ordinary `/wiki/` page and reads the transcription out of
the rendered HTML:

- **Units are paragraphs** (`<p>`), their text as transcribed, whitespace
  collapsed. The page header, page labels and anything Wikisource marks
  `noprint` or `ws-noexport` are not the transcription and are skipped.
- **Printed page breaks** come from the `page break` template: each becomes a
  zero-width `page` mark at its exact place, so a quotation is located by
  page even when a paragraph runs across two. A paragraph's reference names
  the page it starts on and its place in the discourse ("JD 1:50 ¶12"); its
  locator gives the pages it spans.
- **The page run must be whole.** An edition's `select.pages` names the first
  and last page; a missing or out-of-order break fails the parse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, ClassVar

from dilemma import catalog
from dilemma.sources import register
from research_engine_sdk.units import Unit, UnitDocument, UnitMark

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dilemma.fetch import Stored
    from dilemma.sources import AcquireContext

_SKIP_CLASSES = ("noprint", "ws-noexport", "mw-editsection", "reference")
_VOID = frozenset({"br", "img", "link", "meta", "hr", "input", "wbr", "source", "col"})
_PAGE_ID = re.compile(r"^page(\d+)$")


class WikisourceError(ValueError):
    """The page does not hold a transcription in the shape expected."""


@dataclass
class _Paragraph:
    parts: list[str] = field(default_factory=list)
    #: (offset in the collapsed text, page number) for each break inside it.
    breaks: list[tuple[int, int]] = field(default_factory=list)

    def length(self) -> int:
        return len(_collapse("".join(self.parts)))


class _Transcription(HTMLParser):
    """Paragraphs and page breaks from a rendered Wikisource page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[_Paragraph] = []
        self.loose_breaks: list[int] = []  # breaks before the next paragraph
        self._stack: list[tuple[str, bool]] = []  # (tag, skipping)
        self._inside = False
        self._paragraph: _Paragraph | None = None

    def _skipping(self) -> bool:
        return any(skip for _, skip in self._stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if not self._inside:
            if "mw-parser-output" in classes:
                self._inside = True
                self._stack.append((tag, False))
            return
        # The break's page label sits in a noprint span, so it is read from
        # the label's id even while its text is being skipped.
        page = _PAGE_ID.match(attributes.get("id") or "")
        if page:
            self._break(int(page.group(1)))
        if tag in _VOID:
            return
        skip = tag in {"style", "script", "sup"} or any(c in classes for c in _SKIP_CLASSES)
        self._stack.append((tag, skip))
        if tag == "p" and not self._skipping():
            self._paragraph = _Paragraph()

    def _break(self, page: int) -> None:
        if self._paragraph is not None:
            self._paragraph.breaks.append((self._paragraph.length(), page))
        else:
            self.loose_breaks.append(page)

    def handle_endtag(self, tag: str) -> None:
        if not self._inside or tag in _VOID:
            return
        while self._stack:
            open_tag, _ = self._stack.pop()
            if open_tag == "p" and self._paragraph is not None:
                if _collapse("".join(self._paragraph.parts)).strip():
                    if self.loose_breaks:
                        self._paragraph.breaks[:0] = [(0, p) for p in self.loose_breaks]
                        self.loose_breaks = []
                    self.paragraphs.append(self._paragraph)
                self._paragraph = None
            if open_tag == tag:
                break
        if not self._stack:
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside and self._paragraph is not None and not self._skipping():
            self._paragraph.parts.append(data)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@register
class Wikisource:
    keys: ClassVar[tuple[str, ...]] = ("jd-1852-04-09-young",)
    parser: ClassVar[str] = "dilemma.wikisource"
    parser_version: ClassVar[str] = "1"
    #: How each edition's references are written, and its volume.
    REFS: ClassVar[dict[str, tuple[str, int]]] = {"jd-1852-04-09-young": ("JD", 1)}

    async def acquire(self, context: AcquireContext) -> list[Stored]:
        url = context.entry.acquire.url
        assert url, f"{context.entry.key} has no URL in the catalog"
        return [await context.fetcher.fetch(url, source_key=context.entry.key, ext="html")]

    def parse(self, key: str, artifacts: list[Stored]) -> Iterable[UnitDocument]:
        (artifact,) = artifacts
        _, entry = catalog.entry(key)
        prefix, volume = self.REFS[key]
        yield discourse(
            key, artifact.path.read_text(encoding="utf-8"), prefix=prefix, volume=volume,
            title=" ".join(entry.title.split()), pages=entry.select.get("pages"),
        )


def discourse(
    key: str, html: str, *, prefix: str, volume: int, title: str,
    pages: list[int] | None = None,
) -> UnitDocument:
    """One document from a transcribed discourse page."""
    reader = _Transcription()
    reader.feed(html)
    reader.close()
    if not reader.paragraphs:
        raise WikisourceError(f"{key}: no transcribed paragraphs found")
    seen = [page for p in reader.paragraphs for _, page in p.breaks]
    if pages:
        first, last = pages
        if seen != list(range(first, last + 1)):
            raise WikisourceError(f"{key}: page breaks {seen}, expected {first}–{last}")
    units = []
    page = seen[0] if seen else None
    for number, paragraph in enumerate(reader.paragraphs, start=1):
        raw = _collapse("".join(paragraph.parts))
        lead = len(raw) - len(raw.lstrip())
        text = raw.strip()
        marks: list[UnitMark] = []
        start_page = page
        for offset, at in paragraph.breaks:
            position = min(max(offset - lead, 0), len(text))
            if position == 0:
                start_page = at
            marks.append(UnitMark("page", position, position, {"page": at}))
            page = at
        units.append(
            Unit(
                ref=f"{prefix} {volume}:{start_page} ¶{number}",
                text=text,
                kind="paragraph",
                marks=tuple(marks),
                locator=_locator(volume, start_page, page, number),
            )
        )
    return UnitDocument(
        title=title, source=f"dilemma:{key}", units=tuple(units),
        document_type="source_document", node_type="discourse",
        metadata={"volume": volume, "pages": [seen[0], seen[-1]] if seen else None},
    )


def _locator(volume: int, first: Any, last: Any, paragraph: int) -> dict[str, Any]:
    return {"volume": volume, "page_start": first, "page_end": last, "paragraph": paragraph}
