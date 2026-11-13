"""Read one captured page: inventory it, then read every text region.

The smallest honest version of the reader stage (WP2 adds the second and third
recognizers, alignment and the marks). What matters here is the shape: the
inventory decides *what* is read, each region is read independently, and the ink
the inventory could not place stays reported as gaps for as long as the page
lives.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Protocol

from .readers.tesseract import RegionReading, read_region
from .regions import Box, Region, inventory


class RegionReader(Protocol):
    def __call__(self, image, box: Box, *, region_id: str) -> RegionReading: ...


@dataclass(frozen=True)
class ReadRegion:
    """One region of the inventory, and what the reader made of it."""

    region: Region
    reading: RegionReading


@dataclass(frozen=True)
class PageReading:
    """What one page yielded: the regions read, and the ink nobody could place."""

    capture_id: str
    regions: tuple[ReadRegion, ...]
    gaps: tuple[Box, ...]
    columns: int
    seconds: float

    @property
    def text(self) -> str:
        return "\n".join(item.reading.text for item in self.regions if not item.reading.empty)


def read_page(image_bytes: bytes, *, capture_id: str, reader: RegionReader = read_region) -> PageReading:
    from PIL import Image

    started = time.monotonic()
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    found = inventory(image)
    readings: list[ReadRegion] = []
    for index, region in enumerate(found.text_regions):
        readings.append(
            ReadRegion(
                region=region,
                reading=reader(image, region.box, region_id=f"{capture_id}:r{index}:{region.kind}"),
            )
        )
    return PageReading(
        capture_id=capture_id,
        regions=tuple(readings),
        gaps=found.gaps,
        columns=found.columns,
        seconds=time.monotonic() - started,
    )
