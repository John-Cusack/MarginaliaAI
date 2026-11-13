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


class ReaderFailed(RuntimeError):
    """A reader raised while reading a page.

    After a preflight has passed, a reader that *raises* is a failure of the run,
    not an empty reading: the pipeline must not go on to store a page it read with
    one reader down, because nothing in the result would say so.
    """

    def __init__(self, reader: str, error: str) -> None:
        self.reader = reader
        self.error = error
        super().__init__(f"reader {reader} failed: {error}")


@dataclass(frozen=True)
class Preflight:
    """Whether one configured reader loaded and ran, before any page is read."""

    reader: str
    ok: bool
    error: str = ""
    output_chars: int = 0


#: The probe every reader is asked to read before a job: a synthetic page with two
#: bars of ink, generated here and never a captured screen.
PROBE_SIZE = (240, 80)


def probe_image():
    """A tiny synthetic page: white, with two black bars where text would be."""
    from PIL import Image, ImageDraw

    width, height = PROBE_SIZE
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([10, 20, width - 10, 32], fill="black")
    draw.rectangle([10, 48, width - 60, 60], fill="black")
    return image


def preflight(readers: Readers) -> tuple[Preflight, ...]:
    """Load every configured reader and read one synthetic region with it.

    The rule this exists for: a per-region empty reading is legitimate only when
    the reader is *loaded and ran*. A reader that cannot load - a missing model, a
    missing binary, an unmet quantization dependency - would otherwise contribute
    silence to every page, and the run would silently become an N-1 reader run
    whose gate number means something else. So the reader is asked to read once
    before the job starts, and the answer decides whether the job may run.
    """
    box = (0.0, 0.0, float(PROBE_SIZE[0]), float(PROBE_SIZE[1]))
    verdicts: list[Preflight] = []
    for name, reader in readers.named().items():
        if reader is None:
            continue
        try:
            reading = reader.read(probe_image(), box, region_id=f"preflight:{name}")
            verdicts.append(
                Preflight(
                    reader=name,
                    ok=True,
                    output_chars=len((reading.text or "").strip()),
                )
            )
        except Exception as exc:  # a model that will not load, a binary that is absent
            verdicts.append(Preflight(reader=name, ok=False, error=f"{type(exc).__name__}: {exc}"))
    return tuple(verdicts)


class ReadersUnavailable(RuntimeError):
    """One or more configured readers did not load.

    Raised before any page is read, so the caller can block the job (the app) or
    refuse to report a run (the eval). A run whose readers are not all loaded is a
    different run, and its numbers mean something else.
    """

    def __init__(self, unavailable: tuple[Preflight, ...]) -> None:
        self.unavailable = unavailable
        super().__init__(
            "; ".join(f"reader {v.reader} unavailable: {v.error}" for v in unavailable)
        )


def require_readers(readers: Readers) -> tuple[Preflight, ...]:
    """Preflight the readers, and refuse to go on if one of them did not load."""
    verdicts = preflight(readers)
    unavailable = tuple(verdict for verdict in verdicts if not verdict.ok)
    if unavailable:
        raise ReadersUnavailable(unavailable)
    return verdicts


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


class _Failures:
    """The first reader error of a page, so it can be raised after the page."""

    def __init__(self) -> None:
        self.first: tuple[str, str] | None = None

    def record(self, reader: str, error: str) -> None:
        if self.first is None:
            self.first = (reader, error)


def _read_region(reader: object, reader_name: str, image, box: Box, region_id: str, failures: _Failures) -> str:
    """One reader's text for one region.

    An empty *answer* is fine and is recorded as empty; a *failure* is recorded and
    raised once the page is done, because the difference between the two is the
    difference between a reader that looked and saw nothing and a reader that was
    never there.
    """
    try:
        return reader.read(image, box, region_id=region_id).text
    except Exception as exc:
        failures.record(reader_name, f"{type(exc).__name__}: {exc}")
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
    failures = _Failures()
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    found: Inventory = inventory(image)
    stored: list[StoredRegion] = []
    confirmations: list[Confirmation] = []
    marks: list[Mark] = []
    gaps: list[Mark] = []
    words = words_marked = 0

    for index, region in enumerate(found.text_regions):
        texts = {
            name: _read_region(reader, name, image, region.box, f"{capture_id}:r{index}:{name}", failures)
            if reader is not None
            else ""
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

    if failures.first is not None:
        # Raised here, after the inventory and the confirm work, so the caller can
        # block the job with the reader and the error and store none of it.
        raise ReaderFailed(*failures.first)

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
