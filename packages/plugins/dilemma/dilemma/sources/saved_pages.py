"""Pages the operator saved by hand, from sites whose terms forbid robots.

The Church's terms of use (which also govern the Joseph Smith Papers) forbid
any automated access but allow viewing and downloading for personal,
noncommercial use. So the pack never fetches these pages: the operator opens
each one, saves it as HTML into the edition's inbox
(`<data_dir>/inbox/<edition_key>/`), and this reader takes it from there.

- **Gospel Topics essays and conference talks** (churchofjesuschrist.org
  study pages): units are the body's paragraphs, in order, located by their
  ordinal. Footnote markers are dropped from the text; the notes themselves
  are the page's apparatus, not the essay.
- **Joseph Smith Papers transcripts** are read once a saved sample shows
  their structure; until then a saved transcript fails with that message
  rather than being guessed at.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import TYPE_CHECKING, ClassVar

from dilemma import catalog
from dilemma.sources import register
from research_engine_sdk.units import Unit, UnitDocument

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dilemma.fetch import Stored
    from dilemma.sources import AcquireContext

_SKIP_CLASSES = ("marker", "note-ref", "footnote", "related-content")


class SavedPageError(ValueError):
    """The saved file is not a page this reader knows how to read."""


class _StudyBody(HTMLParser):
    """Paragraphs of a churchofjesuschrist.org study page's body block."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self.title: str | None = None
        self._depth = 0  # nesting depth inside the body block; 0 = outside
        self._skip = 0
        self._paragraph: list[str] | None = None
        self._title: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "h1" and self.title is None:
            self._title = []
        if self._depth == 0:
            if "body-block" in classes:
                self._depth = 1
            return
        if tag in {"br", "img", "wbr"}:
            return
        self._depth += 1
        if self._skip or tag in {"sup", "style", "script"} or any(
            c in classes for c in _SKIP_CLASSES
        ):
            self._skip += 1
        if tag == "p" and not self._skip:
            self._paragraph = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._title is not None:
            self.title = re.sub(r"\s+", " ", "".join(self._title)).strip() or None
            self._title = None
        if self._depth == 0 or tag in {"br", "img", "wbr"}:
            return
        if self._skip:
            self._skip -= 1
        elif tag == "p" and self._paragraph is not None:
            text = re.sub(r"\s+", " ", "".join(self._paragraph)).strip()
            if text:
                self.paragraphs.append(text)
            self._paragraph = None
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._title is not None:
            self._title.append(data)
        if self._depth and not self._skip and self._paragraph is not None:
            self._paragraph.append(data)


@register
class SavedPages:
    keys: ClassVar[tuple[str, ...]] = (
        "gte-becoming-like-god",
        "gte-race-and-the-priesthood",
        "gte-book-of-abraham",
        "gte-plural-marriage",
        "gte-first-vision-accounts",
        "gc-1976-10-kimball-our-own-liahona",
        "lectures-on-faith-1835",
        "kfd-bullock",
        "kfd-richards",
        "kfd-woodruff",
        "kfd-clayton",
    )
    parser: ClassVar[str] = "dilemma.saved_pages"
    parser_version: ClassVar[str] = "1"

    async def acquire(self, context: AcquireContext) -> list[Stored]:
        return [
            context.fetcher.keep_file(path, source_key=context.entry.key)
            for path in context.inbox_files(".html", ".htm")
        ]

    def parse(self, key: str, artifacts: list[Stored]) -> Iterable[UnitDocument]:
        _, entry = catalog.entry(key)
        if "josephsmithpapers.org" in (entry.acquire.url or ""):
            raise SavedPageError(
                f"{key}: the Joseph Smith Papers transcript reader is written once a "
                f"saved transcript shows its structure; the file is kept in the raw "
                f"store meanwhile"
            )
        if len(artifacts) != 1:
            raise SavedPageError(f"{key}: expected one saved page, found {len(artifacts)}")
        yield study_page(key, artifacts[0].path.read_text(encoding="utf-8"),
                         fallback_title=entry.title)


def study_page(key: str, html: str, *, fallback_title: str) -> UnitDocument:
    """One document from a saved churchofjesuschrist.org study page."""
    reader = _StudyBody()
    reader.feed(html)
    reader.close()
    if not reader.paragraphs:
        raise SavedPageError(
            f"{key}: no body-block paragraphs found; is this the saved study page?"
        )
    units = tuple(
        Unit(ref=f"¶{n}", text=text, kind="paragraph", locator={"paragraph": n})
        for n, text in enumerate(reader.paragraphs, start=1)
    )
    return UnitDocument(
        title=reader.title or fallback_title,
        source=f"dilemma:{key}",
        units=units,
        document_type="source_document",
        node_type="essay" if key.startswith("gte-") else "talk",
        metadata={"saved_by": "operator"},
    )
