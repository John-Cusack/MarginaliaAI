"""Render a work's body with footnotes derived from the corpus.

Display strings are built here and only here: author, title and year come
from document metadata, the tier from verification, and nothing is ever
written back to the file. A footnote with any part missing names the document
it came from and wears `[provisional]` so a reader never mistakes a guess
for a record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from research_engine.services.works.files import WorkFileReader, strip_definition_lines

if TYPE_CHECKING:
    from pathlib import Path

    from research_engine.domain.works_files import CitationEntry

logger = structlog.get_logger()

#: Locator keys with a conventional rendering. Anything else renders as
#: `key value`, so a pack-specific locator still reads sensibly.
_LOCATOR_WORDS = {"volume": "vol.", "page": "p.", "chapter": "ch.", "verse": "v."}


class WorkRenderer:
    def __init__(self, documents: Any, verification: Any, works_dir: Path) -> None:
        self._documents = documents
        self._verification = verification
        self._reader = WorkFileReader(works_dir)

    async def render(self, work_path: str) -> dict[str, Any]:
        work = self._reader.read(work_path)
        entries = sorted(
            work.front_matter.citations, key=lambda entry: int(entry.id[1:])
        )
        footnotes = [await self._footnote(entry) for entry in entries]
        body = strip_definition_lines(work.body).rstrip()
        if footnotes:
            rendered = body + "\n\n" + "\n".join(
                f"[^{note['id']}]: {note['text']}" for note in footnotes
            ) + "\n"
        else:
            rendered = work.body
        return {"rendered": rendered, "footnotes": footnotes}

    async def _footnote(self, entry: CitationEntry) -> dict[str, Any]:
        document = await self._documents.get(entry.document_id)
        metadata = dict(document.metadata) if document is not None else {}
        if document is not None and document.title and "title" not in metadata:
            metadata.setdefault("title", document.title)

        author = metadata.get("author")
        title = metadata.get("title")
        year = _year(metadata.get("year", metadata.get("date")))
        provisional = author is None or title is None or year is None

        shown_author = str(author) if author is not None else f"document {entry.document_id}"
        shown_title = f"*{title}*" if title is not None else f"document {entry.document_id}"
        text = f"{shown_author}, {shown_title}"
        if entry.edition:
            text += f", {entry.edition}"
        locator = dict(entry.locator or {})
        volume = locator.pop("volume", None)
        if volume is not None:
            text += f", vol. {volume}"
        if year is not None:
            text += f" ({year})"
        for key, value in locator.items():
            text += f", {_LOCATOR_WORDS.get(key, key)} {value}"

        result = await self._verification.verify(entry.quoted_text, entry.document_id)
        text += f". [{result.tier.value}]"
        if provisional:
            text += " [provisional]"
        return {
            "id": entry.id,
            "text": text,
            "provisional": provisional,
            "tier": result.tier.value,
        }


def _year(value: Any) -> str | None:
    """A year from a metadata `year` or `date` value.

    Dates arrive as full ISO strings; the footnote wants the year. Four
    leading digits are the year, anything else is passed through untouched so
    a non-date never silently becomes one.
    """
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 4 and text[:4].isdigit() and (len(text) == 4 or not text[4].isdigit()):
        return text[:4]
    return text or None
