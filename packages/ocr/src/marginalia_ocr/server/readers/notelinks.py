"""Note links: a pure-blue box, re-read alone, upscaled, whitelisted.

Design §2's measurements, which are the parameters here: the boxes are pure blue;
re-reading each alone and upscaled with a ``0123456789[]`` whitelist at psm 7 got
bracketed references 8/8 at 1x, while superscripts need 2x (8/8 at 2x, about half
wrong at 1x). So the link is read on its own at 2x, with the whitelist that makes a
digit-or-bracket answer the only possible one.

A link the whitelist cannot read is reported as a gap on that link: the text around
it is unaffected, and a note number nobody can read is not a note number to invent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

Box = tuple[float, float, float, float]

#: §2: the whitelist that made bracketed references work.
WHITELIST = "0123456789[]"
#: §2: superscripts need 2x; bracketed refs were already right at 1x.
UPSCALE = 2
PSM_SINGLE_LINE = 7
#: A pure-blue box: blue well above both other channels, and bright enough to be a
#: link rather than dark ink that happens to be blue-ish.
BLUE_LEAD = 40
BLUE_FLOOR = 120

BRACKETED = re.compile(r"^\[?\d{1,3}\]?$")


@dataclass(frozen=True)
class NoteLink:
    """One link box, and what reading it produced."""

    box: Box
    text: str
    confidence: float = 0.0
    gap_reason: str = ""

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    @property
    def bracketed(self) -> bool:
        return bool(BRACKETED.match(self.text.strip()))


def blue_boxes(image, *, ink_level: int = 200, minimum_pixels: int = 12) -> tuple[Box, ...]:
    """Pure-blue components: the boxes a note link is drawn in."""
    import numpy as np
    from scipy import ndimage

    rgb = np.asarray(image.convert("RGB")).astype(int)
    grey = np.asarray(image.convert("L"))
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mask = (
        (blue > red + BLUE_LEAD)
        & (blue > green + BLUE_LEAD)
        & (blue > BLUE_FLOOR)
        & (grey < ink_level)
    )
    labels, _count = ndimage.label(mask, structure=np.ones((3, 3), dtype=int))
    boxes: list[Box] = []
    for index, size in enumerate(np.bincount(labels.ravel())):
        if index == 0 or size < minimum_pixels:
            continue
        rows, cols = np.where(labels == index)
        boxes.append((float(cols.min()), float(rows.min()), float(cols.max() + 1), float(rows.max() + 1)))
    return tuple(boxes)


def read_link(image, box: Box, *, region_id: str = "link", command: str = "tesseract") -> NoteLink:
    """Read one link box alone, upscaled, with the whitelist."""
    import pytesseract
    from pytesseract import Output

    from .tesseract import available

    if not available(command):
        return NoteLink(box=box, text="", gap_reason=f"{command} is not on PATH")

    x0, y0, x1, y1 = (int(round(value)) for value in box)
    crop = image.crop((x0, y0, x1, y1))
    crop = crop.resize((crop.width * UPSCALE, crop.height * UPSCALE))
    config = f"--psm {PSM_SINGLE_LINE} -c tessedit_char_whitelist={WHITELIST}"
    data = pytesseract.image_to_data(
        crop, lang="eng", config=config, output_type=Output.DICT
    )
    words = [
        (text, float(confidence))
        for text, confidence in zip(data.get("text", []), data.get("conf", []), strict=False)
        if text.strip() and float(confidence) >= 0
    ]
    text = "".join(word for word, _confidence in words).replace(" ", "")
    confidence = min((value for _word, value in words), default=0.0)
    if not text.strip():
        return NoteLink(box=box, text="", gap_reason="the whitelist read nothing")
    if not BRACKETED.match(text):
        return NoteLink(box=box, text=text, confidence=confidence, gap_reason="not a number or bracket")
    return NoteLink(box=box, text=text, confidence=confidence)


def read_links(image, boxes: Sequence[Box] | None = None, **kwargs) -> tuple[NoteLink, ...]:
    found = tuple(boxes) if boxes is not None else blue_boxes(image)
    return tuple(read_link(image, box, region_id=f"link-{index}", **kwargs) for index, box in enumerate(found))
