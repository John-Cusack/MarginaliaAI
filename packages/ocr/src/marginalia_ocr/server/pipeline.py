"""Read one captured page with three readers, and say what is confirmed.

The order is the design's: inventory the page (every mark accounted for), read
every text region with each reader independently, confirm word by word, run the
checks, and keep the marks and gaps. A reader that fails is an *empty* reading -
it is not agreement (7.2), and the confirm step's fallback spine means two working
readers still produce text with a mark saying the third said nothing.

Nothing here decides to forget: a page ends up confirmed, marked or gapped, and
each of those is a row the assessment can read.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .readers.tesseract import RegionReading

from .confirm import (
    Confirmation,
    Mark,
    Vocabulary,
    check_spans_for,
    confirm_readings,
)
from .readers.notelinks import NoteLink, blue_boxes, read_link
from .regions import Inventory, Region, inventory

Box = tuple[float, float, float, float]

#: The readers the pipeline knows, and the order a spine falls back through when
#: the configured primary produced nothing. Tesseract first: it is the measured
#: best on prose, and the VLM is only second because it is the model that reads
#: what Tesseract cannot.
READER_NAMES = ("tesseract", "vlm", "third")


class RegionReader(Protocol):
    def read(self, image, box: Box, *, region_id: str) -> RegionReading: ...


@dataclass
class Readers:
    """The three readers, in the order the pipeline names them."""

    vlm: object
    tesseract: object
    third: object

    def named(self) -> dict[str, object]:
        return {"vlm": self.vlm, "tesseract": self.tesseract, "third": self.third}

    def unload(self) -> None:
        """Give back anything resident: the GPU is shared."""
        for reader in (self.vlm, self.tesseract, self.third):
            unload = getattr(reader, "unload", None)
            if callable(unload):
                unload()


@dataclass(frozen=True)
class StoredRegion:
    region_id: str
    kind: str
    box: Box
    readings: tuple[tuple[str, str], ...]
    status: str
    reason: str
    text: str


@dataclass
class PageOutcome:
    capture_id: str
    status: str
    reason: str
    text: str
    marks: tuple[Mark, ...] = ()
    gaps: tuple[Mark, ...] = ()
    regions: tuple[StoredRegion, ...] = ()
    links: tuple[NoteLink, ...] = ()
    columns: int = 1
    words: int = 0
    words_marked: int = 0
    seconds: float = 0.0
    confirmations: tuple[Confirmation, ...] = field(default=(), repr=False)


def _read_or_empty(reader: object, image, box: Box, region_id: str) -> str:
    """A reader that fails says nothing, and saying nothing is not agreement."""
    try:
        return reader.read(image, box, region_id=region_id).text
    except Exception:  # a missing model, a missing binary, an OOM
        return ""


def read_page(
    image_bytes: bytes,
    *,
    capture_id: str,
    readers: Readers,
    vocabulary: Vocabulary | None = None,
    primary: str = "tesseract",
    dictionary: frozenset[str] | None = None,
    chooser: object | None = None,
    reread: object | None = None,
    read_note_links: bool = True,
) -> PageOutcome:
    """Read one page: inventory, three readings per region, confirm, check."""
    from PIL import Image

    started = time.monotonic()
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    found: Inventory = inventory(image)
    stored: list[StoredRegion] = []
    confirmations: list[Confirmation] = []
    marks: list[Mark] = []
    gaps: list[Mark] = []
    words = words_marked = 0

    for index, region in enumerate(found.text_regions):
        texts = {
            name: _read_or_empty(reader, image, region.box, f"{capture_id}:r{index}:{name}")
            for name, reader in readers.named().items()
        }
        primary_text = texts.get(primary, "")
        # The vocabulary check needs a vocabulary. With none yet - the front
        # matter of a book, before a single word is confirmed - it would call every
        # word unmatched and mark every page, so it waits until the book has words
        # of its own; the dictionary check still applies.
        known = vocabulary.words if vocabulary else frozenset()
        checks_for_region = (
            tuple(
                (name, spans)
                for name, spans in check_spans_for(primary_text, vocabulary=known, dictionary=dictionary)
                if name != "vocabulary" or known
            )
            if primary_text.strip()
            else ()
        )
        confirmation = confirm_readings(
            texts,
            primary=primary,
            checks=checks_for_region,
            chooser=chooser,
            book=vocabulary,
            reread=reread,
            region_has_ink=_region_has_ink(found, region),
        )
        confirmations.append(confirmation)
        marks.extend(confirmation.marks)
        gaps.extend(
            Mark(span=gap.span, kind="gap", reason=gap.reason, candidates=())
            for gap in confirmation.gaps
        )

        region_words = len(confirmation.text.split())
        marked_words = sum(
            len(confirmation.text[mark.span[0] : mark.span[1]].split()) for mark in confirmation.marks
        )
        words += region_words
        words_marked += marked_words
        status = "gapped" if confirmation.gaps else ("marked" if confirmation.marks else "confirmed")
        stored.append(
            StoredRegion(
                region_id=f"{capture_id}:r{index}",
                kind=region.kind,
                box=region.box,
                readings=tuple(texts.items()),
                status=status,
                reason=(confirmation.gaps[0].reason if confirmation.gaps else (confirmation.marks[0].reason if confirmation.marks else "")),
                text=confirmation.text,
            )
        )
        if vocabulary is not None and confirmation.confirmed_spans:
            for start, end in confirmation.confirmed_spans:
                vocabulary.add_confirmed(confirmation.text[start:end])

    links: tuple[NoteLink, ...] = ()
    if read_note_links:
        links = tuple(read_link(image, box, region_id=f"link-{i}") for i, box in enumerate(blue_boxes(image)))
        for link in links:
            if link.empty:
                gaps.append(Mark(span=(0, 0), kind="gap", reason=f"note link unreadable: {link.gap_reason}"))

    if gaps:
        status, reason = "gapped", gaps[0].reason
    elif marks:
        status, reason = "marked", marks[0].reason
    else:
        status, reason = "confirmed", ""
    return PageOutcome(
        capture_id=capture_id,
        status=status,
        reason=reason,
        text="\n".join(region.text for region in stored),
        marks=tuple(marks),
        gaps=tuple(gaps),
        regions=tuple(stored),
        links=links,
        columns=found.columns,
        words=words,
        words_marked=words_marked,
        seconds=time.monotonic() - started,
        confirmations=tuple(confirmations),
    )


def _region_has_ink(found: Inventory, region: Region) -> bool:
    """Does the inventory say this region carries ink?

    Every connected mark was assigned to a region or reported as a gap, so a text
    region with no ink is not a region at all - and a reader that produced nothing
    for a region that *does* carry ink has failed, which is a different fact.
    """
    return region.ink > 0
