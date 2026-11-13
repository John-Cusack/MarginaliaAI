"""The synthetic corpus: Kindle-like pages with exact truth.

Every page is generated from a :class:`PageSpec`, laid out at 1x, and rasterized
at each requested DPR. The result carries three things that the rest of Phase 0
scores against:

* ``truth_text`` — the canonical text the pipeline is supposed to end up with,
  in reading order, with chrome excluded and tables in their Markdown form;
  ``spans`` maps every readable region onto its character range in that text.
* ``regions``/``chrome`` — typed boxes with lines, so a reader's region
  inventory, line counts, note links and gaps can be scored.
* ``png`` — bytes only. Nothing is written to disk; screens are measured in the
  process that generated them.

The pages cover what the design says breaks OCR: two columns with a gutter,
spanning headings, single-column and chapter-opener pages, notes with raised
numbers and blue bracketed links, URLs, TOC and sparse pages, tables that
continue across a screen boundary, a figure with text, the "Back to NNN" popup,
and arabic, roman and Location-only footers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from . import text as T
from .fonts import FontChoice, resolve
from .layout import (
    PAGE_H,
    PAGE_W,
    Block,
    FigureBand,
    Fonts,
    LinkToken,
    PageSpec,
    Region,
    TableBand,
    TextBand,
    layout_page,
)
from .render import render_png
from .seeded_errors import words_of

if TYPE_CHECKING:
    from .degrade import Degrade


@dataclass(frozen=True)
class TextSpan:
    """A readable region's character range in ``Screen.truth_text``."""

    region_index: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class Screen:
    screen_id: str
    kind: str
    dpr: int
    blur: float
    width: int
    height: int
    png: bytes
    image_sha256: str
    page_label: str
    label_kind: str
    scrubber: int
    position_start: int
    position_end: int
    truth_text: str
    regions: tuple[Region, ...]
    chrome: tuple[Region, ...]
    spans: tuple[TextSpan, ...]
    font: dict[str, object]
    degrade: dict[str, object] | None = None

    @property
    def layout_lines(self) -> tuple[tuple[int, int, int, int], ...]:
        """Textline rectangles at 1x: the ``layout_data`` stand-in for binding."""
        return tuple(line.box for region in self.regions for line in region.lines)

    def frame_geometry(self) -> tuple[object, ...]:
        """Truth and geometry, independent of rasterization.

        The DPR and blur tests compare this: a page drawn at another DPR, or
        softened, must still describe the same page.
        """
        return (
            self.truth_text,
            tuple(region.box for region in self.regions),
            tuple(region.box for region in self.chrome),
            tuple(self.layout_lines),
        )

    def record(self) -> dict[str, object]:
        """What a capture harness records beside an image (no text, no badge)."""
        return {
            "screen_id": self.screen_id,
            "kind": self.kind,
            "dpr": self.dpr,
            "width": self.width,
            "height": self.height,
            "image_sha256": self.image_sha256,
            "page_label": self.page_label,
            "label_kind": self.label_kind,
            "scrubber": self.scrubber,
            "page_data": {"start": self.position_start, "end": self.position_end},
            "layout_lines": [list(box) for box in self.layout_lines],
        }


def load_fonts(choice: FontChoice | None = None) -> tuple[Fonts, FontChoice]:
    from PIL import ImageFont

    from .layout import SIZES

    chosen = choice or resolve()
    faces = {style: ImageFont.truetype(str(chosen.path), size) for style, size in SIZES.items()}
    return Fonts(path=str(chosen.path), sha256=chosen.sha256, faces=faces), chosen


def _note_blocks(notes: tuple[tuple[int, str], ...]) -> tuple[Block, ...]:
    blocks: list[Block] = []
    for number, note in notes:
        lead = f"{number} "
        links = tuple(
            LinkToken(token=token, at=len(lead) + note.index(token))
            for token in ("[AR2]", "[AR7]")
            if token in note
        )
        blocks.append(Block(text=f"{lead}{note}", style="note", links=links, superscript_lead=True))
    return tuple(blocks)


def page_specs(variant: int = 0) -> tuple[PageSpec, ...]:
    """The catalog, one spec per layout class, optionally with varied prose.

    ``variant`` rotates the *prose pool* the prose pages are built from, which is
    what a gate about the share of clean screens needs: more pages of the same
    classes rather than the same page counted twice. Variant 0 is byte-for-byte
    what this always produced, so every earlier measurement still describes the
    same screens. The pages that depend on specific words - the split-word page, a
    table's cells, the TOC - are left alone, because their text is the point of
    them.
    """
    pooled = T.PROSE if not variant else T.PROSE[variant % len(T.PROSE):] + T.PROSE[: variant % len(T.PROSE)]
    prose = tuple(Block(text=paragraph) for paragraph in pooled)
    specs: list[PageSpec] = []

    specs.append(
        PageSpec(
            page_id="chapter-opener",
            kind="chapter_opener",
            label=T.FOOTER_PAGE_TEMPLATE.format(page="vi", total=368),
            label_kind="roman",
            position_start=6,
            position_end=18,
            running_head=T.RUNNING_HEADS[0],
            bands=(
                TextBand(
                    blocks=(Block(text="1. A Quarrel with Small Decisions", style="heading"),),
                    span=True,
                    kind="spanning_heading",
                ),
                TextBand(blocks=(prose[0],), kind="single_column", span=True),
            ),
        )
    )
    specs.append(
        PageSpec(
            page_id="two-column",
            kind="two_column_prose",
            label=T.FOOTER_PAGE_TEMPLATE.format(page=42, total=368),
            label_kind="arabic",
            position_start=504,
            position_end=516,
            running_head=T.RUNNING_HEADS[1],
            bands=(TextBand(blocks=prose),),
        )
    )
    specs.append(
        PageSpec(
            page_id="spanning-heading",
            kind="spanning_heading",
            label=T.FOOTER_PAGE_TEMPLATE.format(page=43, total=368),
            label_kind="arabic",
            position_start=516,
            position_end=528,
            running_head=T.RUNNING_HEADS[1],
            bands=(
                TextBand(
                    blocks=(Block(text="2. The Margin as Evidence", style="heading"),),
                    span=True,
                    kind="spanning_heading",
                ),
                TextBand(blocks=prose),
            ),
        )
    )
    specs.append(
        PageSpec(
            page_id="notes",
            kind="notes_page",
            label=T.FOOTER_PAGE_TEMPLATE.format(page=44, total=368),
            label_kind="arabic",
            position_start=528,
            position_end=540,
            running_head=T.RUNNING_HEADS[1],
            bands=(TextBand(blocks=_note_blocks(T.NOTES)),),
        )
    )
    specs.append(
        PageSpec(
            page_id="two-column-notes",
            kind="two_column_notes",
            label=T.FOOTER_PAGE_TEMPLATE.format(page=45, total=368),
            label_kind="arabic",
            position_start=540,
            position_end=552,
            running_head=T.RUNNING_HEADS[1],
            bands=(TextBand(blocks=_note_blocks(T.NOTES)),),
        )
    )
    specs.append(
        PageSpec(
            page_id="toc",
            kind="toc_page",
            label=T.FOOTER_PAGE_TEMPLATE.format(page="ix", total=368),
            label_kind="roman",
            position_start=108,
            position_end=120,
            bands=(
                TextBand(
                    blocks=tuple(
                        Block(text=label, style="toc", page=page) for label, page in T.TOC_ENTRIES
                    ),
                    span=True,
                    kind="toc_page",
                ),
            ),
        )
    )
    specs.append(
        PageSpec(
            page_id="location-only",
            kind="location_page",
            label=T.FOOTER_LOCATION_TEMPLATE.format(location=1234, total=9999),
            label_kind="location",
            position_start=1234,
            position_end=1246,
            running_head=T.RUNNING_HEADS[0],
            bands=(TextBand(blocks=(prose[3], prose[4]), kind="single_column", span=True),),
        )
    )
    table = TableBand(
        caption=T.TABLE_CAPTION,
        number=T.TABLE_NUMBER,
        header=T.TABLE_HEADER,
        rows=T.TABLE_ROWS,
    )
    specs.append(
        PageSpec(
            page_id="table",
            kind="table_page",
            label=T.FOOTER_PAGE_TEMPLATE.format(page=118, total=368),
            label_kind="arabic",
            position_start=1416,
            position_end=1428,
            running_head=T.RUNNING_HEADS[1],
            bands=(TextBand(blocks=(prose[2],)), table),
        )
    )
    specs.append(
        PageSpec(
            page_id="table-continuation",
            kind="table_continuation",
            label=T.FOOTER_PAGE_TEMPLATE.format(page=119, total=368),
            label_kind="arabic",
            position_start=1428,
            position_end=1440,
            running_head=T.RUNNING_HEADS[1],
            bands=(
                TableBand(
                    caption=f"{T.TABLE_CAPTION} (continued)",
                    number=T.TABLE_NUMBER,
                    header=T.TABLE_HEADER,
                    rows=T.TABLE_ROWS_CONTINUATION,
                    continuation=True,
                ),
                TextBand(blocks=(prose[5],), kind="single_column", span=True),
            ),
        )
    )
    specs.append(
        PageSpec(
            page_id="figure",
            kind="figure_page",
            label=T.FOOTER_PAGE_TEMPLATE.format(page=77, total=368),
            label_kind="arabic",
            position_start=924,
            position_end=936,
            running_head=T.RUNNING_HEADS[1],
            bands=(
                TextBand(
                    blocks=(Block(text="3. Signed Decimal Places", style="heading"),),
                    span=True,
                    kind="spanning_heading",
                ),
                FigureBand(caption=T.FIGURE_CAPTION, labels=T.FIGURE_LABELS),
                TextBand(blocks=(prose[5],), kind="single_column", span=True),
            ),
        )
    )
    specs.append(
        PageSpec(
            page_id="sparse",
            kind="sparse_page",
            label=T.FOOTER_PAGE_TEMPLATE.format(page=164, total=368),
            label_kind="arabic",
            position_start=1968,
            position_end=1980,
            bands=(
                TextBand(
                    blocks=(Block(text="5. What the Audit Cannot See", style="heading"),),
                    span=True,
                    kind="spanning_heading",
                ),
                TextBand(
                    blocks=(
                        Block(text="https://example.invalid/series/1990-2019", style="body"),
                        Block(text="[AR2] https://example.invalid/vintages/archive", style="body"),
                    ),
                    span=True,
                    kind="single_column",
                ),
            ),
        )
    )
    specs.append(
        PageSpec(
            page_id="popup",
            kind="popup_page",
            label=T.FOOTER_PAGE_TEMPLATE.format(page=41, total=368),
            label_kind="arabic",
            position_start=492,
            position_end=504,
            running_head=T.RUNNING_HEADS[0],
            bands=(TextBand(blocks=prose),),
            popup_location=312,
        )
    )
    split_at = T.PROSE[5].index("transfor") + len("transfor")
    specs.append(
        PageSpec(
            page_id="split-word",
            kind="split_word_page",
            label=T.FOOTER_PAGE_TEMPLATE.format(page=211, total=368),
            label_kind="arabic",
            position_start=2532,
            position_end=2544,
            running_head=T.RUNNING_HEADS[0],
            bands=(
                TextBand(
                    blocks=(Block(text=T.PROSE[5], hard_breaks=(split_at,)),),
                    span=True,
                    kind="single_column",
                ),
            ),
        )
    )
    # More table pages, so the cell-by-cell audit has a population worth a bound:
    # the operator asked for at least thirty planted cell errors, and two table
    # screens cannot carry them. Same shape as the first table, different sizes.
    for index, (rows, columns) in enumerate(((3, 3), (4, 4), (6, 3), (7, 4), (5, 3), (8, 3)), start=1):
        table_rows = tuple(
            tuple(
                f"{T.TABLE_ROWS[row % len(T.TABLE_ROWS)][column % len(T.TABLE_ROWS[0])]}"
                for column in range(columns)
            )
            for row in range(rows)
        )
        specs.append(
            PageSpec(
                page_id=f"table-extra-{index}",
                kind="table_page",
                label=T.FOOTER_PAGE_TEMPLATE.format(page=200 + index, total=368),
                label_kind="arabic",
                position_start=2000 + index * 20,
                position_end=2012 + index * 20,
                running_head=T.RUNNING_HEADS[1],
                bands=(
                    TextBand(blocks=(prose[2],)),
                    TableBand(
                        caption=T.TABLE_CAPTION,
                        number=f"{T.TABLE_NUMBER}.{index}",
                        header=T.TABLE_HEADER[:columns],
                        rows=table_rows,
                    ),
                ),
            )
        )
    if variant:
        # A variant is a different page and says so: a screen id that repeats would
        # make two pages look like one in every record and every count.
        specs = [
            replace(
                spec,
                page_id=f"{spec.page_id}-v{variant}",
                position_start=spec.position_start + variant * 10_000,
                position_end=spec.position_end + variant * 10_000,
            )
            for spec in specs
        ]
    return tuple(specs)


def line_boxes(screen: Screen) -> list[tuple[float, float, float, float]]:
    """Every line rectangle on the screen, in reading order (the layout_data stand-in)."""
    return [line.box for region in screen.regions for line in region.lines]


def run_words(screen: Screen) -> list[tuple[str, int]]:
    """Every run's word, in reading order, with the index of the line it sits on."""
    sequence: list[tuple[str, int]] = []
    line_index = 0
    for region in screen.regions:
        for line in region.lines:
            for run in line.runs:
                for word in words_of(run.text):
                    sequence.append((word.text, line_index))
            line_index += 1
    return sequence


def build_corpus(
    *,
    font: FontChoice | None = None,
    dprs: tuple[int, ...] = (1, 2, 3),
    blur: float = 0.0,
    kinds: tuple[str, ...] | None = None,
    degrade: Degrade | None = None,
    variants: int = 1,
) -> tuple[Screen, ...]:
    """Lay out, rasterize and record every page at every DPR.

    ``variants`` > 1 adds prose-varied variants of the same classes: a gate about
    the share of clean screens needs more screens, and more *pages* is the only
    honest way to get them.
    """
    fonts, chosen = load_fonts(font)
    screens: list[Screen] = []
    for spec in tuple(spec for variant in range(max(1, variants)) for spec in page_specs(variant)):
        if kinds is not None and spec.kind not in kinds:
            continue
        layout = layout_page(spec, fonts)
        truth, spans = _assemble_truth(layout.regions)
        for dpr in dprs:
            png = render_png(layout, fonts, dpr, blur=blur, degrade=degrade)
            screens.append(
                Screen(
                    screen_id=f"{spec.page_id}@{dpr}x" + (f"+{degrade.label}" if degrade else ""),
                    kind=spec.kind,
                    dpr=dpr,
                    blur=blur,
                    width=PAGE_W * dpr,
                    height=PAGE_H * dpr,
                    png=png,
                    image_sha256=hashlib.sha256(png).hexdigest(),
                    page_label=spec.label,
                    label_kind=spec.label_kind,
                    scrubber=spec.position_start,
                    position_start=spec.position_start,
                    position_end=spec.position_end,
                    truth_text=truth,
                    regions=layout.regions,
                    chrome=layout.chrome,
                    spans=spans,
                    font=chosen.record(),
                    degrade=degrade.record() if degrade else None,
                )
            )
    return tuple(screens)


def _assemble_truth(regions: tuple[Region, ...]) -> tuple[str, tuple[TextSpan, ...]]:
    parts: list[str] = []
    spans: list[TextSpan] = []
    offset = 0
    for index, region in enumerate(regions):
        if index:
            offset += 2  # "\n\n"
        spans.append(TextSpan(region_index=index, char_start=offset, char_end=offset + len(region.text)))
        parts.append(region.text)
        offset += len(region.text)
    return "\n\n".join(parts), tuple(spans)


def reading_order_text(regions: tuple[Region, ...]) -> str:
    """Truth text for an arbitrary region sequence (used by tests)."""
    return _assemble_truth(regions)[0]
