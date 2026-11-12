"""Page geometry, wrapping and region inventory for the synthetic corpus.

A page is a list of *bands* laid out top to bottom. A text band is either
two-column (blocks flow into the left column, then the right) or spanning
(single column across the body width) — the two shapes the pixel-gutter split
in the design has to get right. Table and figure bands are spanning.

Invariants this module guarantees, and the tests rely on:

* Coordinates and font sizes are 1x. The renderer multiplies both by the DPR,
  which is what a browser does when it rasterizes a page; the truth record
  keeps 1x coordinates and states the DPR beside them.
* A block never splits between columns: column assignment is block-granular.
  Region text is therefore a whole-paragraph view of the page, and a region's
  text maps cleanly onto the page's truth text.
* Line boxes are tight glyph boxes, computed with the same anchor the renderer
  draws with. They are used three ways: to verify that no ink falls outside a
  declared region, as the stand-in for the reader's ``layout_data`` textline
  rectangles when the capture binding check is measured, and to place typed
  sub-regions (blue note links) inside a line.
* Chrome — running head, footer, popup — is never part of the reading order, so
  a reader that leaks it onto the page can be caught.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .text import HYPHEN_BREAKS

Box = tuple[int, int, int, int]

PAGE_W = 1280
PAGE_H = 780

MARGIN_L = 64
MARGIN_R = 64
BODY_TOP = 100
BODY_BOTTOM = 700
BAND_GAP = 18
GUTTER = 48

BODY_W = PAGE_W - MARGIN_L - MARGIN_R
COL_W = (BODY_W - GUTTER) // 2

HEAD_BAND: Box = (0, 30, PAGE_W, 86)
FOOTER_BAND: Box = (0, 712, PAGE_W, PAGE_H)
POPUP_BOX: Box = (330, 186, 950, 278)

LINE_H = 22
ROW_H = 30
FIGURE_H = 150

SIZES: dict[str, int] = {
    "body": 16,
    "heading": 19,
    "note": 13,
    "super": 9,
    "toc": 14,
    "caption": 13,
    "table": 13,
    "figure_text": 13,
    "chrome": 12,
}

UNDERLINE_PX = 2
SUPERSCRIPT_LIFT = 5


class LayoutOverflow(RuntimeError):
    """A page spec does not fit its page: the spec is wrong, not the font."""


@dataclass(frozen=True)
class Fonts:
    """One loaded face per style, at 1x sizes."""

    path: str
    sha256: str
    faces: dict[str, Any]

    def face(self, style: str) -> Any:
        return self.faces[style]

    def measure(self, style: str, text: str) -> float:
        return float(self.faces[style].getlength(text))


@dataclass(frozen=True)
class TextRun:
    """One drawn text placement at 1x; the renderer scales x, baseline and size."""

    text: str
    x: int
    baseline: int
    style: str
    link: bool = False


@dataclass(frozen=True)
class Line:
    text: str
    box: Box
    runs: tuple[TextRun, ...] = ()


@dataclass(frozen=True)
class SubRegion:
    """A typed run inside a region's text, e.g. a blue note link."""

    kind: str
    box: Box
    char_start: int
    char_end: int


@dataclass(frozen=True)
class Region:
    kind: str
    box: Box
    lines: tuple[Line, ...] = ()
    text: str = ""
    sub: tuple[SubRegion, ...] = ()
    cells: tuple[tuple[str, ...], ...] = ()
    caption: str = ""
    detail: str = ""
    # The ruled area of a table, when the caption sits outside the grid.
    grid: Box | None = None

    @property
    def line_count(self) -> int:
        return len(self.lines)


@dataclass(frozen=True)
class LinkToken:
    """A bracketed reference drawn as a blue underlined box inside a block."""

    token: str
    at: int


@dataclass(frozen=True)
class Block:
    text: str
    style: str = "body"
    page: str | None = None
    links: tuple[LinkToken, ...] = ()
    superscript_lead: bool = False
    # Character offsets at which the page breaks a word with a hyphen: the drawn
    # head keeps the hyphen, the canonical text keeps the whole word.
    hard_breaks: tuple[int, ...] = ()


@dataclass(frozen=True)
class TextBand:
    blocks: tuple[Block, ...]
    span: bool = False
    kind: str = "text_column"


@dataclass(frozen=True)
class TableBand:
    caption: str
    number: str
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    continuation: bool = False


@dataclass(frozen=True)
class FigureBand:
    caption: str
    labels: tuple[str, ...]


Band = TextBand | TableBand | FigureBand


@dataclass(frozen=True)
class PageSpec:
    page_id: str
    kind: str
    label: str
    label_kind: str
    position_start: int
    position_end: int
    bands: tuple[Band, ...]
    running_head: str | None = None
    popup_location: int | None = None


@dataclass(frozen=True)
class Layout:
    """A laid-out page: readable regions in reading order, then chrome."""

    regions: tuple[Region, ...]
    chrome: tuple[Region, ...]

    def all_regions(self) -> tuple[Region, ...]:
        return self.regions + self.chrome


# --------------------------------------------------------------------------- #
# wrapping
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Word:
    text: str
    start: int
    end: int
    link: LinkToken | None = None
    hard_break_after: bool = False
    # Drawn with no space before it: punctuation that follows a link token.
    glue: bool = False


@dataclass(frozen=True)
class _Piece:
    text: str
    word: _Word
    head_of_split: bool = False


def split_words(
    text: str,
    links: tuple[LinkToken, ...] = (),
    hard_breaks: tuple[int, ...] = (),
) -> list[_Word]:
    """Split on single spaces, keeping each word's character span in ``text``.

    Offsets in ``hard_breaks`` split the word containing them: the head piece is
    drawn with a trailing hyphen and forces a break after it, while the word's
    canonical span stays whole.
    """
    by_pos = {link.at: link for link in links}
    cuts = sorted(hard_breaks)
    words: list[_Word] = []
    index = 0
    for raw in text.split(" "):
        start = index
        end = start + len(raw)
        index = end + 1
        if not raw:
            continue
        link = by_pos.get(start)
        if link is not None and end > start + len(link.token):
            # A bracketed reference followed by punctuation: the box covers the
            # token, the punctuation sits outside it and is drawn glued on.
            stop = start + len(link.token)
            words.append(_Word(text=link.token, start=start, end=stop, link=link))
            words.append(_Word(text=text[stop:end], start=stop, end=end, glue=True))
            continue
        inside = [offset for offset in cuts if start < offset < end]
        if not inside:
            words.append(_Word(text=raw, start=start, end=end, link=link))
            continue
        previous = start
        for offset in [*inside, end]:
            head = offset < end
            words.append(
                _Word(
                    text=text[previous:offset] + ("-" if head else ""),
                    start=previous,
                    end=offset,
                    hard_break_after=head,
                )
            )
            previous = offset
    return words


def wrap_block(
    block: Block,
    fonts: Fonts,
    width: int,
    *,
    breaks: dict[str, tuple[str, str]] | None = None,
) -> list[list[_Piece]]:
    """Wrap one block into lines of pieces that fit ``width``.

    A word wider than the column is either split at a declared break point (the
    head keeps its hyphen, as a page draws it) or reported as an overflow: the
    corpus never silently clips text.
    """
    breaks = breaks or {}
    lines: list[list[_Piece]] = []
    current: list[_Piece] = []
    used = 0.0
    space = fonts.measure(block.style, " ")

    def flush() -> None:
        nonlocal current, used
        if current:
            lines.append(current)
        current = []
        used = 0.0

    for word in split_words(block.text, block.links, block.hard_breaks):
        need = fonts.measure(block.style, word.text)
        if current and not word.glue and used + space + need > width:
            split = None if word.link else breaks.get(word.text)
            if split is not None and used + space + fonts.measure(block.style, split[0]) <= width:
                current.append(_Piece(text=split[0], word=word, head_of_split=True))
                flush()
                current.append(_Piece(text=split[1], word=word))
                used = fonts.measure(block.style, split[1])
                continue
            flush()
        if not current and need > width:
            raise LayoutOverflow(f"{word.text!r} is wider than {width}px at this size")
        gap = 0.0 if (word.glue or not current) else space
        current.append(_Piece(text=word.text, word=word))
        used = used + gap + need
        if word.hard_break_after:
            flush()
    flush()
    return lines


def _line_text(pieces: list[_Piece]) -> str:
    """The drawn line's text: a space between words, none before glued runs."""
    out = ""
    for index, piece in enumerate(pieces):
        if index and not piece.word.glue:
            out += " "
        out += piece.text
    return out


def _gap_after(pieces: list[_Piece], index: int, space: float) -> float:
    """Space to advance after ``pieces[index]``: none before a glued run."""
    if index + 1 < len(pieces) and pieces[index + 1].word.glue:
        return 0.0
    return space


# --------------------------------------------------------------------------- #
# placement
# --------------------------------------------------------------------------- #


def _glyph_box(font: Any, text: str, x: float, baseline: float) -> Box:
    """Tight box of ``text`` drawn at ``(x, baseline)`` with anchor ``ls``."""
    left, top, right, bottom = font.getbbox(text, anchor="ls")
    return (int(x + left), int(baseline + top), int(x + right), int(baseline + bottom))


def _union(boxes: list[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _centered_box(text: str, fonts: Fonts, style: str, baseline: float) -> Box:
    width = fonts.measure(style, text)
    return _glyph_box(fonts.face(style), text, (PAGE_W - width) / 2, baseline)


@dataclass(frozen=True)
class _Flow:
    """Wrapped lines for one block, with the block's offset in a region's text."""

    block: Block
    lines: list[list[_Piece]]
    offset: int


def _prepare(blocks: tuple[Block, ...], fonts: Fonts, width: int) -> list[_Flow]:
    flows: list[_Flow] = []
    offset = 0
    for index, block in enumerate(blocks):
        if index:
            offset += 2  # the "\n\n" that joins paragraph text
        flows.append(
            _Flow(
                block=block,
                lines=wrap_block(block, fonts, width, breaks=HYPHEN_BREAKS),
                offset=offset,
            )
        )
        offset += len(block.text)
    return flows


def _build_region(
    flows: list[_Flow],
    fonts: Fonts,
    x: int,
    y_top: int,
    kind: str,
    width: int = COL_W,
) -> Region:
    """Draw the given flows into one column at ``x``."""
    lines: list[Line] = []
    sub: list[SubRegion] = []
    baseline = float(y_top)
    for flow in flows:
        block = flow.block
        for line_index, pieces in enumerate(flow.lines):
            baseline += LINE_H
            if baseline > BODY_BOTTOM:
                raise LayoutOverflow(f"column overflow in {kind} at x={x}")
            pen = float(x)
            boxes: list[Box] = []
            runs: list[TextRun] = []
            for piece_index, piece in enumerate(pieces):
                superscript = bool(block.superscript_lead and line_index == 0 and piece_index == 0)
                style = "super" if superscript else block.style
                lift = SUPERSCRIPT_LIFT if superscript else 0
                box = _glyph_box(fonts.face(style), piece.text, pen, baseline - lift)
                runs.append(
                    TextRun(
                        text=piece.text,
                        x=int(pen),
                        baseline=int(baseline - lift),
                        style=style,
                        link=piece.word.link is not None,
                    )
                )
                if piece.word.link is not None:
                    if piece.head_of_split:
                        raise LayoutOverflow("a note link may not be split across lines")
                    sub.append(
                        SubRegion(
                            kind="note_link",
                            box=(box[0], box[1], box[2], box[3] + UNDERLINE_PX),
                            char_start=flow.offset + piece.word.start,
                            char_end=flow.offset + piece.word.end,
                        )
                    )
                boxes.append(box)
                pen += fonts.measure(style, piece.text) + _gap_after(
                    pieces, piece_index, fonts.measure(style, " ")
                )
            line_box = _union(boxes)
            if any(piece.word.link is not None for piece in pieces):
                # The underline is drawn ink: the line box must contain it.
                line_box = (line_box[0], line_box[1], line_box[2], line_box[3] + UNDERLINE_PX)
            lines.append(Line(text=_line_text(pieces), box=line_box, runs=tuple(runs)))
    region_text = "\n\n".join(flow.block.text for flow in flows)
    box = _union([line.box for line in lines]) if lines else (x, y_top, x + width, y_top)
    return Region(kind=kind, box=box, lines=tuple(lines), text=region_text, sub=tuple(sub))


def _place_columns(band: TextBand, fonts: Fonts, y_top: int) -> tuple[Region, ...]:
    """Flow a band's blocks into the left column, then the right."""
    flows = _prepare(band.blocks, fonts, COL_W)
    columns: list[tuple[int, list[_Flow]]] = []
    pending = list(flows)
    while pending and len(columns) < 2:
        x = (MARGIN_L, MARGIN_L + COL_W + GUTTER)[len(columns)]
        here: list[_Flow] = []
        used = 0
        while pending and used + len(pending[0].lines) * LINE_H <= BODY_BOTTOM - y_top:
            flow = pending.pop(0)
            here.append(flow)
            used += len(flow.lines) * LINE_H
        if not here and pending:
            raise LayoutOverflow(f"first block of {band.kind} does not fit a column")
        columns.append((x, here))
    if pending:
        raise LayoutOverflow(f"{len(pending)} block(s) of {band.kind} do not fit the page")
    return tuple(
        _build_region(here, fonts, x, y_top, band.kind) for x, here in columns if here
    )


def _place_spanning(band: TextBand, fonts: Fonts, y_top: int) -> Region:
    """A full-width band: a spanning heading, a single-column body, or a TOC."""
    lines: list[Line] = []
    block_texts: list[str] = []
    baseline = float(y_top)
    for block in band.blocks:
        block_texts.append(f"{block.text} {block.page}" if block.page else block.text)
        for pieces in wrap_block(block, fonts, BODY_W, breaks=HYPHEN_BREAKS):
            baseline += LINE_H
            if baseline > BODY_BOTTOM:
                raise LayoutOverflow(f"spanning overflow in {band.kind}")
            pen = float(MARGIN_L)
            boxes: list[Box] = []
            runs: list[TextRun] = []
            for piece_index, piece in enumerate(pieces):
                box = _glyph_box(fonts.face(block.style), piece.text, pen, baseline)
                boxes.append(box)
                runs.append(
                    TextRun(
                        text=piece.text,
                        x=int(pen),
                        baseline=int(baseline),
                        style=block.style,
                        link=piece.word.link is not None,
                    )
                )
                pen += fonts.measure(block.style, piece.text) + _gap_after(
                    pieces, piece_index, fonts.measure(block.style, " ")
                )
            line_box = _union(boxes)
            if block.page is not None:
                page_x = MARGIN_L + BODY_W - fonts.measure(block.style, block.page)
                page_box = _glyph_box(fonts.face(block.style), block.page, page_x, baseline)
                runs.append(
                    TextRun(text=block.page, x=int(page_x), baseline=int(baseline), style=block.style)
                )
                line_box = (MARGIN_L, line_box[1], MARGIN_L + BODY_W, max(line_box[3], page_box[3]))
            lines.append(Line(text=_line_text(pieces), box=line_box, runs=tuple(runs)))
    region_text = "\n\n".join(block_texts)
    box = _union([line.box for line in lines])
    return Region(kind=band.kind, box=box, lines=tuple(lines), text=region_text)


def markdown_table(header: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> str:
    """The canonical text form of a table: a Markdown pipe table (design §8)."""
    head = "| " + " | ".join(header) + " |"
    rule = "| " + " | ".join("---" for _ in header) + " |"
    body = ["| " + " | ".join(cell if cell else " " for cell in row) + " |" for row in rows]
    return "\n".join([head, rule, *body])


def _place_table(band: TableBand, fonts: Fonts, y_top: int) -> tuple[Region, int]:
    cols = len(band.header)
    caption_h = LINE_H if band.caption else 0
    height = caption_h + (len(band.rows) + 1) * ROW_H
    box = (MARGIN_L, y_top, PAGE_W - MARGIN_R, y_top + height)

    lines: list[Line] = []
    if band.caption:
        baseline = y_top + LINE_H
        lines.append(
            Line(
                text=band.caption,
                box=_glyph_box(fonts.face("caption"), band.caption, MARGIN_L, baseline),
                runs=(TextRun(text=band.caption, x=MARGIN_L, baseline=baseline, style="caption"),),
            )
        )
    grid_y = y_top + caption_h
    col_w = (box[2] - box[0]) // cols
    cells = (band.header, *band.rows)
    for row_index, row in enumerate(cells):
        y = grid_y + row_index * ROW_H
        row_runs: list[TextRun] = []
        row_boxes: list[Box] = []
        for col_index, cell in enumerate(row):
            if not cell:
                continue
            x = box[0] + col_index * col_w + 10
            baseline = y + LINE_H
            row_runs.append(TextRun(text=cell, x=x, baseline=baseline, style="table"))
            row_boxes.append(_glyph_box(fonts.face("table"), cell, x, baseline))
        if row_boxes:
            lines.append(
                Line(
                    text=" | ".join(cell for cell in row),
                    box=_union(row_boxes),
                    runs=tuple(row_runs),
                )
            )
    text = (
        f"{band.caption}\n\n{markdown_table(band.header, band.rows)}"
        if band.caption
        else markdown_table(band.header, band.rows)
    )
    return (
        Region(
            kind="table",
            box=box,
            lines=tuple(lines),
            text=text,
            cells=tuple(tuple(row) for row in cells),
            caption=band.caption,
            detail="continuation" if band.continuation else "",
            grid=(box[0], grid_y, box[2], box[3]),
        ),
        box[3],
    )


def _place_figure(band: FigureBand, fonts: Fonts, y_top: int) -> tuple[Region, int]:
    box = (MARGIN_L, y_top, PAGE_W - MARGIN_R, y_top + FIGURE_H)
    lines: list[Line] = []
    baseline = float(y_top + 14)
    inner_x = MARGIN_L + 18
    for text, style in [(label, "figure_text") for label in band.labels] + [(band.caption, "caption")]:
        baseline += LINE_H if style == "figure_text" else LINE_H + 4
        box_line = _glyph_box(fonts.face(style), text, inner_x, baseline)
        lines.append(
            Line(
                text=text,
                box=box_line,
                runs=(TextRun(text=text, x=inner_x, baseline=int(baseline), style=style),),
            )
        )
    text = "\n".join([*band.labels, band.caption])
    return Region(kind="figure", box=box, lines=tuple(lines), text=text), box[3]


def layout_page(spec: PageSpec, fonts: Fonts) -> Layout:
    """Lay a page out at 1x: readable regions in reading order, plus chrome."""
    regions: list[Region] = []
    y = BODY_TOP
    for band in spec.bands:
        if isinstance(band, TextBand):
            if band.span:
                region = _place_spanning(band, fonts, y)
                regions.append(region)
                # A heading belongs to the text below it: no gap after it.
                y = region.box[3] + (0 if band.kind == "spanning_heading" else BAND_GAP)
            else:
                placed = _place_columns(band, fonts, y)
                regions.extend(placed)
                y = max(r.box[3] for r in placed) + BAND_GAP
        elif isinstance(band, TableBand):
            region, bottom = _place_table(band, fonts, y)
            regions.append(region)
            y = bottom + BAND_GAP
        else:
            region, bottom = _place_figure(band, fonts, y)
            regions.append(region)
            y = bottom + BAND_GAP

    chrome: list[Region] = []
    if spec.running_head:
        baseline = HEAD_BAND[1] + 34
        box = _centered_box(spec.running_head, fonts, "chrome", baseline)
        chrome.append(
            Region(
                kind="chrome",
                box=box,
                lines=(
                    Line(
                        text=spec.running_head,
                        box=box,
                        runs=(
                            TextRun(
                                text=spec.running_head,
                                x=int(box[0]),
                                baseline=baseline,
                                style="chrome",
                            ),
                        ),
                    ),
                ),
                text=spec.running_head,
                detail="running_head",
            )
        )
    footer_baseline = FOOTER_BAND[1] + 34
    footer_box = _centered_box(spec.label, fonts, "chrome", footer_baseline)
    chrome.append(
        Region(
            kind="chrome",
            box=footer_box,
            lines=(
                Line(
                    text=spec.label,
                    box=footer_box,
                    runs=(
                        TextRun(
                            text=spec.label,
                            x=int(footer_box[0]),
                            baseline=footer_baseline,
                            style="chrome",
                        ),
                    ),
                ),
            ),
            text=spec.label,
            detail="footer",
        )
    )
    if spec.popup_location is not None:
        popup_text = f"Back to {spec.popup_location}"
        width = fonts.measure("chrome", popup_text)
        baseline = (POPUP_BOX[1] + POPUP_BOX[3]) // 2 + 4
        x = int((POPUP_BOX[0] + POPUP_BOX[2] - width) / 2)
        chrome.append(
            Region(
                kind="chrome",
                box=POPUP_BOX,
                lines=(
                    Line(
                        text=popup_text,
                        box=_glyph_box(fonts.face("chrome"), popup_text, x, baseline),
                        runs=(
                            TextRun(text=popup_text, x=x, baseline=baseline, style="chrome"),
                        ),
                    ),
                ),
                text=popup_text,
                detail="popup",
            )
        )
    return Layout(regions=tuple(regions), chrome=tuple(chrome))
