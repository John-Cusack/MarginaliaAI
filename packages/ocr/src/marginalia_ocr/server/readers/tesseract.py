"""Tesseract, psm 6, one region at a time, with word boxes and confidences.

Region by region, not page by page: a page-level read mixes columns, drops the
order of a spanning heading and gives one confidence for the whole thing. psm 6
treats the crop as a single uniform block of text, which is what a region is.

Two rules that matter more than the code: word boxes come back in *page*
coordinates (the crop's offset is added, never dropped), and an empty or failed
reading is reported as empty - it never becomes agreement (design 7.2).
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass

Box = tuple[float, float, float, float]

#: The whitelist Tesseract's own docs recommend for prose; keep it explicit so a
#: change here is a decision rather than a default.
DEFAULT_PSM = 6


class ReaderUnavailable(RuntimeError):
    """The binary is not installed. A missing reader is a gap, not empty text."""


@dataclass(frozen=True)
class Word:
    text: str
    box: Box
    confidence: float


@dataclass(frozen=True)
class RegionReading:
    region_id: str
    text: str
    words: tuple[Word, ...]
    seconds: float
    empty: bool

    @property
    def character_count(self) -> int:
        return len(self.text)


def available(command: str = "tesseract") -> bool:
    return shutil.which(command) is not None


def words_from_data(data: dict[str, list], offset: tuple[float, float] = (0.0, 0.0)) -> tuple[Word, ...]:
    """Tesseract's word table as words in page coordinates.

    Kept a pure function so the mapping can be tested without the binary: the
    offset arithmetic and the confidence filter are where a reader quietly lies.
    """
    words: list[Word] = []
    left, top = offset
    for index, text in enumerate(data.get("text", [])):
        text = (text or "").strip()
        confidence = float(data["conf"][index]) if index < len(data.get("conf", [])) else -1.0
        if not text or confidence < 0:
            continue  # -1 is Tesseract's "no confidence", not a low one
        words.append(
            Word(
                text=text,
                box=(
                    left + float(data["left"][index]),
                    top + float(data["top"][index]),
                    left + float(data["left"][index]) + float(data["width"][index]),
                    top + float(data["top"][index]) + float(data["height"][index]),
                ),
                confidence=confidence,
            )
        )
    return tuple(words)


def read_region(
    image,
    box: Box,
    *,
    region_id: str,
    psm: int = DEFAULT_PSM,
    lang: str = "eng",
    command: str = "tesseract",
) -> RegionReading:
    """Read one region. An empty answer is an empty reading, never agreement."""
    import pytesseract
    from pytesseract import Output

    if not available(command):
        raise ReaderUnavailable(f"{command} is not on PATH")
    pytesseract.pytesseract.tesseract_cmd = command

    x0, y0, x1, y1 = (int(round(value)) for value in box)
    crop = image.crop((x0, y0, x1, y1))
    started = time.monotonic()
    data = pytesseract.image_to_data(crop, lang=lang, config=f"--psm {psm}", output_type=Output.DICT)
    seconds = time.monotonic() - started

    words = words_from_data(data, offset=(float(x0), float(y0)))
    text = " ".join(word.text for word in words)
    return RegionReading(
        region_id=region_id,
        text=text,
        words=words,
        seconds=seconds,
        empty=not text.strip(),
    )


class TesseractReader:
    """The region function as a reader, so all three share one interface."""

    def __init__(self, *, psm: int = DEFAULT_PSM, lang: str = "eng", command: str = "tesseract") -> None:
        self.psm = psm
        self.lang = lang
        self.command = command

    def read(self, image, box, *, region_id: str):
        return read_region(
            image, box, region_id=region_id, psm=self.psm, lang=self.lang, command=self.command
        )

    def unload(self) -> None:
        """Nothing resident: Tesseract is a subprocess, not a model."""
