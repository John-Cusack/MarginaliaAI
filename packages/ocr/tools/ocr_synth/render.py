"""Rasterize a laid-out page to PNG bytes, in memory.

The renderer does what a browser does when it rasterizes at a device pixel
ratio: the same layout drawn with font sizes and coordinates multiplied by the
DPR. Nothing here touches the disk — the corpus is generated, measured and
discarded inside one process, which is also how the real capture path is
required to work.

Text is drawn at its recorded 1x baseline with anchor ``ls``, the same anchor
the layout uses to compute glyph boxes, so a line's recorded box is the box of
the pixels actually drawn. Note links are pure blue and underlined, because the
note-link specialist detects blue boxes; body text is black on white.
"""

from __future__ import annotations

import io
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

if TYPE_CHECKING:
    from .degrade import Degrade

from .layout import (
    PAGE_H,
    PAGE_W,
    SIZES,
    UNDERLINE_PX,
    Fonts,
    Layout,
    Region,
)

INK = (0, 0, 0)
PAPER = (255, 255, 255)
LINK_BLUE = (0, 0, 238)
RULE = (70, 70, 70)
POPUP_FILL = (232, 232, 232)
POPUP_BORDER = (90, 90, 90)
FIGURE_INSET = 12


@lru_cache(maxsize=64)
def _font(path: str, size: int) -> Any:
    return ImageFont.truetype(path, size)


def scaled_faces(fonts: Fonts, dpr: int) -> dict[str, Any]:
    """One face per style at the DPR-scaled point size."""
    return {style: _font(fonts.path, size * dpr) for style, size in SIZES.items()}


def _draw_kerned(draw: Any, xy: tuple[float, float], text: str, face: Any, fill: Any, kern: float) -> None:
    """Draw a run letter by letter, advancing each glyph by ``kern`` extra pixels.

    Tight kerning is how print puts ``rn`` and ``m`` in the same place: it is the
    one degradation that makes every reader fail the same way, and it cannot be
    expressed with a font's own metrics.
    """
    x, y = xy
    for character in text:
        draw.text((x, y), character, font=face, fill=fill, anchor="ls")
        x += face.getlength(character) + kern


def render_png(
    layout: Layout,
    fonts: Fonts,
    dpr: int,
    blur: float = 0.0,
    degrade: Degrade | None = None,
) -> bytes:
    """Rasterize the page at ``dpr``, optionally blurred and optionally degraded.

    ``degrade`` bends the ink (§"degraded ink") without touching ``layout``: the
    canonical text and every rectangle stay exactly as they were, so a degraded
    screen measures the reader against the same truth a clean one does.
    """
    image = Image.new("RGB", (PAGE_W * dpr, PAGE_H * dpr), PAPER)
    draw = ImageDraw.Draw(image)
    faces = scaled_faces(fonts, dpr)

    for region in layout.all_regions():
        _draw_furniture(draw, region, dpr)
        for line in region.lines:
            for run in line.runs:
                face = faces[run.style]
                ink = LINK_BLUE if run.link else INK
                text = run.text
                if degrade is not None:
                    text, size = degrade.drawn(text, SIZES[run.style])
                    if not run.link:
                        ink = degrade.ink if degrade.light else ink
                    if size != SIZES[run.style]:
                        face = _font(fonts.path, max(1, int(size * dpr)))
                if degrade is not None and degrade.kern and not run.link:
                    _draw_kerned(draw, (run.x * dpr, run.baseline * dpr), text, face, ink, degrade.kern * dpr)
                else:
                    draw.text(
                        (run.x * dpr, run.baseline * dpr),
                        text,
                        font=face,
                        fill=ink,
                        anchor="ls",
                    )
                if run.link:
                    y = (run.baseline + UNDERLINE_PX) * dpr
                    draw.line(
                        [(run.x * dpr, y), ((run.x + face.getlength(run.text)) * dpr, y)],
                        fill=LINK_BLUE,
                        width=max(1, dpr // 2),
                    )

    if degrade is not None:
        blur += degrade.blur
    if blur > 0:
        image = image.filter(ImageFilter.GaussianBlur(radius=blur * dpr))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _draw_furniture(draw: ImageDraw.ImageDraw, region: Region, dpr: int) -> None:
    """Rules and panels: the ink that is not text."""
    hairline = max(1, dpr // 2)
    if region.kind == "table":
        x0, y0, x1, y1 = (value * dpr for value in (region.grid or region.box))
        draw.rectangle([x0, y0, x1, y1], outline=RULE, width=hairline)
        rows = len(region.cells) + 1
        cols = len(region.cells[0]) if region.cells else 1
        for index in range(1, rows):
            y = y0 + (y1 - y0) * index / rows
            draw.line([(x0, y), (x1, y)], fill=RULE, width=1)
        for index in range(1, cols):
            x = x0 + (x1 - x0) * index / cols
            draw.line([(x, y0), (x, y1)], fill=RULE, width=1)
    elif region.kind == "figure":
        x0, y0, x1, y1 = (value * dpr for value in region.box)
        draw.rectangle(
            [
                (x0 + FIGURE_INSET * dpr, y0 + FIGURE_INSET * dpr),
                (x1 - FIGURE_INSET * dpr, y1 - FIGURE_INSET * dpr),
            ],
            outline=RULE,
            width=hairline,
        )
    elif region.detail == "popup":
        x0, y0, x1, y1 = (value * dpr for value in region.box)
        draw.rectangle([x0, y0, x1, y1], fill=POPUP_FILL, outline=POPUP_BORDER, width=max(1, dpr))
