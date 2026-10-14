"""Ben Crowder's *Scriptures in JSON*: the 2013 LDS standard works, verse by verse.

One JSON file per volume, each a list of books (or, for the Doctrine and
Covenants, of sections) holding chapters of numbered verses. The repository
declares the verse text public domain and leaves out the copyrighted
footnotes and chapter headings, so what is loaded is the verse text alone,
exactly as the file has it.

One document per chapter (per section, for the D&C), one unit per verse.
References take the form the gold set and vouch seeds use: "1 Nephi 3:7",
"D&C 130:22", "Articles of Faith 1:8".
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from dilemma.sources import register
from research_engine_sdk.units import Unit, UnitDocument

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dilemma.fetch import Stored
    from dilemma.sources import AcquireContext

#: The D&C's reference prefix as the profiles write it.
DC = "D&C"


@register
class ScripturesJson:
    keys: ClassVar[tuple[str, ...]] = ("bom-2013", "dc-2013", "pgp-2013")
    parser: ClassVar[str] = "dilemma.scriptures_json"
    parser_version: ClassVar[str] = "1"

    async def acquire(self, context: AcquireContext) -> list[Stored]:
        url = context.entry.acquire.url
        assert url, f"{context.entry.key} has no URL in the catalog"
        return [await context.fetcher.fetch(url, source_key=context.entry.key, ext="json")]

    def parse(self, key: str, artifacts: list[Stored]) -> Iterable[UnitDocument]:
        (artifact,) = artifacts
        volume = json.loads(artifact.path.read_text(encoding="utf-8"))
        if "sections" in volume:
            for section in volume["sections"]:
                yield _document(key, DC, section["section"], section["verses"], "section")
            return
        for book in volume["books"]:
            for chapter in book["chapters"]:
                yield _document(key, book["book"], chapter["chapter"], chapter["verses"],
                                "chapter")


def _document(
    key: str, book: str, chapter: int, verses: list[dict[str, Any]], node_type: str
) -> UnitDocument:
    units = tuple(
        Unit(
            ref=f"{book} {chapter}:{verse['verse']}",
            label=str(verse["verse"]),
            text=verse["text"],
            locator={"book": book, "chapter": chapter, "verse": verse["verse"]},
        )
        for verse in verses
    )
    return UnitDocument(
        title=f"{book} {chapter}",
        source=f"dilemma:{key}:{book}.{chapter}",
        units=units,
        node_type=node_type,
        metadata={"book": book, "chapter": chapter},
    )
