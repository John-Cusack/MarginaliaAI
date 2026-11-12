"""Font selection for the synthetic corpus.

The Kryptonite reader rasterizes book pages in Amazon's Bookerly, which cannot
be copied or shipped. The generator therefore draws with an installed open
serif chosen from a fixed preference list: CER measured against one serif is
not comparable with CER measured against another, so the chosen file's path and
SHA-256 go into every screen record.

`OCR_SYNTH_FONT` overrides the choice. When it names a file that does not
exist, or nothing in the search path matches, resolution raises instead of
falling back to PIL's bitmap font: a silent fallback would quietly change every
number the corpus produces.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# Preference order. Liberation Serif is metric-compatible with Times New Roman
# (a text face of Bookerly's class); DejaVu Serif is a Vera derivative, wider
# and sturdier, and is the fallback that is present on almost every Linux host.
# Both are redistributable (OFL 1.1 and the Bitstream Vera licence).
PREFERRED: tuple[str, ...] = (
    "EBGaramond-Regular.ttf",
    "SourceSerif4-Regular.ttf",
    "CharisSIL-Regular.ttf",
    "Charter-Regular.ttf",
    "LiberationSerif-Regular.ttf",
    "DejaVuSerif.ttf",
)

SEARCH_DIRS: tuple[str, ...] = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "~/.fonts",
    "~/.local/share/fonts",
)

FONT_ENV = "OCR_SYNTH_FONT"


class FontNotFound(RuntimeError):
    """No usable corpus font; the caller must not fall back to a default face."""


@dataclass(frozen=True)
class FontChoice:
    """The font a corpus run drew with, and the identity that makes it comparable."""

    path: Path
    sha256: str
    size_bytes: int

    @property
    def name(self) -> str:
        return self.path.name

    def record(self) -> dict[str, object]:
        """Fields that go into a screen record and into every report header."""
        return {"name": self.name, "sha256": self.sha256, "bytes": self.size_bytes}


@lru_cache(maxsize=1)
def _installed() -> dict[str, Path]:
    """Map basename -> path for every font under the search directories."""
    found: dict[str, Path] = {}
    for raw in SEARCH_DIRS:
        root = Path(raw).expanduser()
        if not root.is_dir():
            continue
        for path in _walk(root):
            found.setdefault(path.name, path)
    return found


def _walk(root: Path) -> Iterator[Path]:
    for suffix in ("*.ttf", "*.otf", "*.ttc"):
        yield from root.rglob(suffix)


def resolve(path: str | Path | None = None) -> FontChoice:
    """Resolve the corpus font, or raise :class:`FontNotFound`."""
    override = path or os.environ.get(FONT_ENV) or None
    if override is not None:
        chosen = Path(override).expanduser()
        if not chosen.is_file():
            raise FontNotFound(f"{FONT_ENV}={override} is not a file")
        return _choice(chosen)

    installed = _installed()
    for name in PREFERRED:
        if (found := installed.get(name)) is not None:
            return _choice(found)
    wanted = ", ".join(PREFERRED)
    searched = ", ".join(str(Path(d).expanduser()) for d in SEARCH_DIRS)
    raise FontNotFound(f"no corpus font; looked for {wanted} under {searched}")


def _choice(path: Path) -> FontChoice:
    data = path.read_bytes()
    return FontChoice(path=path, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
