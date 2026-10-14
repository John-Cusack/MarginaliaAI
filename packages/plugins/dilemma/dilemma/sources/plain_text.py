"""Operator-provided plain-text books (Kindle scrapes, purchased ebooks).

One `.txt` file per book in the edition's inbox; paragraphs are blank-line
separated. Kindle page-chrome lines (`Back t0 30`) are dropped; everything
else is kept verbatim, OCR quirks included — critical-role text feeds leads
and adjudication prose, never horn evidence.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar

from dilemma import catalog
from dilemma.sources import register
from research_engine_sdk.units import Unit, UnitDocument

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dilemma.fetch import Stored
    from dilemma.sources import AcquireContext

_CHROME_RE = re.compile(r"^Back t[0oO] \d+\s*$")


@register
class PlainTextBook:
    keys: ClassVar[tuple[str, ...]] = ("johnson-sharing-good-news",)
    parser: ClassVar[str] = "dilemma.plain_text"
    parser_version: ClassVar[str] = "1"

    async def acquire(self, context: AcquireContext) -> list[Stored]:
        return [
            context.fetcher.keep_file(path, source_key=context.entry.key)
            for path in context.inbox_files(".txt")
        ]

    def parse(self, key: str, artifacts: list[Stored]) -> Iterable[UnitDocument]:
        _, entry = catalog.entry(key)
        if len(artifacts) != 1:
            raise ValueError(f"{key}: expected one .txt book, found {len(artifacts)}")
        text = artifacts[0].path.read_text(encoding="utf-8")
        paragraphs = [
            chunk.strip()
            for chunk in re.split(r"\n\s*\n", text)
            if chunk.strip() and not _CHROME_RE.match(chunk.strip())
        ]
        if not paragraphs:
            raise ValueError(f"{key}: no paragraphs found")
        yield UnitDocument(
            title=entry.title,
            source=f"dilemma:{key}",
            units=tuple(
                Unit(ref=f"¶{n}", text=text, kind="paragraph", locator={"paragraph": n})
                for n, text in enumerate(paragraphs, start=1)
            ),
            document_type="source_document",
            node_type="book",
            metadata={"saved_by": "operator"},
        )
