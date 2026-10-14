"""Tanzil: the Qur'an's Arabic text and its translations, ayah by ayah.

Tanzil's "Text, with aya numbers" downloads put one ayah per line as
`surah|ayah|text`, followed by comment lines (`#`) giving the version and the
terms of use. Tanzil allows verbatim copies only, so the text is kept as
given: the one transformation is Unicode NFC — the same characters, in their
canonical encoding, so a quotation typed with another encoding still matches
— and it is recorded on every document.

One document per surah, one unit per ayah; the same reference scheme for the
Arabic and every translation, so editions align by reference alone.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, ClassVar

from dilemma.sources import register
from research_engine_sdk.units import Unit, UnitDocument

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dilemma.fetch import Stored
    from dilemma.sources import AcquireContext

#: How references to this work are written.
PREFIX = "Qur'an"
SURAHS = 114


class TanzilError(ValueError):
    """The download is not in the shape Tanzil documents."""


@register
class Tanzil:
    keys: ClassVar[tuple[str, ...]] = (
        "quran-tanzil-uthmani",
        "quran-pickthall-1930",
        "quran-sahih-international",
    )
    parser: ClassVar[str] = "dilemma.tanzil"
    parser_version: ClassVar[str] = "1"

    async def acquire(self, context: AcquireContext) -> list[Stored]:
        url = context.entry.acquire.url
        assert url, f"{context.entry.key} has no URL in the catalog"
        return [await context.fetcher.fetch(url, source_key=context.entry.key, ext="txt")]

    def parse(self, key: str, artifacts: list[Stored]) -> Iterable[UnitDocument]:
        (artifact,) = artifacts
        language = "ar" if key == "quran-tanzil-uthmani" else "en"
        yield from surahs(key, artifact.path.read_text(encoding="utf-8-sig"), language)


def surahs(key: str, text: str, language: str) -> Iterable[UnitDocument]:
    """One document per surah from a `surah|ayah|text` download."""
    comments = [line for line in text.splitlines() if line.startswith("#")]
    by_surah: dict[int, list[Unit]] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            raise TanzilError(f"line {number} is not surah|ayah|text: {line[:60]!r}")
        surah, ayah, body = int(parts[0]), int(parts[1]), parts[2]
        body = unicodedata.normalize("NFC", body).strip()
        if not body:
            raise TanzilError(f"{surah}|{ayah} has no text")
        by_surah.setdefault(surah, []).append(
            Unit(
                ref=f"{PREFIX} {surah}:{ayah}",
                label=str(ayah),
                text=body,
                kind="ayah",
                locator={"surah": surah, "ayah": ayah},
            )
        )
    provenance = {
        "unicode_normalization": "NFC",
        "tanzil_notice": [line.lstrip("# ").rstrip() for line in comments][:40],
    }
    for surah in sorted(by_surah):
        yield UnitDocument(
            title=f"{PREFIX}, surah {surah}",
            source=f"dilemma:{key}:{surah}",
            units=tuple(by_surah[surah]),
            language=language,
            node_type="surah",
            metadata={"surah": surah, **provenance},
        )
