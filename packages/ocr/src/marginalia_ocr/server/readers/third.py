"""The third recognizer: EasyOCR, one region at a time.

**The design does not name one.** §7.2 asks for "a third recognizer with its own
recognition model (EasyOCR per region, or PaddleOCR - not Docling-over-Tesseract),
chosen by how rarely its errors coincide with the other two". Phase 0 measured
EasyOCR, so EasyOCR is what this is - and that is a decision to confirm, not a
conclusion the measurement reached: the coin-check that §7.2 describes (error
coincidence between the three) has not been run, and PaddleOCR is named as an
alternative. Flagged here rather than buried in a dependency list.

It runs on CPU: the GPU belongs to the VLM and to the operator's own work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .tesseract import RegionReading


class ThirdUnavailable(RuntimeError):
    """No easyocr installed: a missing reader is a gap, not empty text."""


@dataclass
class EasyOcrReader:
    """EasyOCR, loaded once per job and read region by region."""

    languages: tuple[str, ...] = ("en",)
    _reader: object | None = field(default=None, init=False, repr=False)

    @property
    def loaded(self) -> bool:
        return self._reader is not None

    def load(self) -> None:
        if self._reader is not None:
            return
        try:
            import easyocr
        except ImportError as exc:  # pragma: no cover - the server extra
            raise ThirdUnavailable(f"easyocr is not installed: {exc}") from exc
        self._reader = easyocr.Reader(list(self.languages), gpu=False, verbose=False)

    def unload(self) -> None:
        self._reader = None

    def read(self, image, box, *, region_id: str) -> RegionReading:
        self.load()
        x0, y0, x1, y1 = (int(round(value)) for value in box)
        crop = image.crop((x0, y0, x1, y1))
        started = time.monotonic()
        import numpy as np

        lines = self._reader.readtext(np.asarray(crop), detail=0, paragraph=False)
        text = " ".join(line.strip() for line in lines if line.strip())
        return RegionReading(
            region_id=region_id, text=text, words=(), seconds=time.monotonic() - started, empty=not text
        )
