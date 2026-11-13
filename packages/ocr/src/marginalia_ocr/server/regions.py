"""Region inventory: every connected ink region assigned to exactly one kind.

Design 7.1, and the reason it exists: *unassigned ink is a gap*. A pipeline that
reads only what it recognises silently drops whatever it did not, so the first
step is an account of every mark on the page - text column, spanning heading,
table, figure, note link, or chrome (the reader's own popup and footer) - and
anything that cannot be placed is reported as a gap rather than forgotten.

Everything here works from pixels. Docling's layout labels are hints (WP2), not
the authority: the gutter is found by projection, a heading is detected rather
than forced into a column, and single-column pages stay single-column.

Coordinates are in the image's own pixels; the caller knows the dpr.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

#: Grey level below which a pixel counts as ink. The corpus renders black on
#: white; a real screen's antialiasing sits well above this.
INK_LEVEL = 200
#: Below this many pixels a component is dust from antialiasing, not a mark.
MIN_COMPONENT_PIXELS = 12
#: The reader's own bars live in the outer eighth of the screen.
CHROME_MARGIN = 0.08
#: A blue channel this far above both others is a link box, not grey text.
BLUE_LEAD = 40
#: Furniture in the margin is a line of small text: a running head, a page
#: number, the scrubber bar. Anything taller than this fraction of the page is a
#: mark, not furniture, and must reach the gap report instead.
FURNITURE_HEIGHT = 0.04

Box = tuple[float, float, float, float]

KINDS = ("text_column", "spanning_heading", "table", "figure", "note_link", "chrome")


@dataclass(frozen=True)
class Line:
    """One text line: its box and how much ink is in it."""

    box: Box
    ink: int


@dataclass(frozen=True)
class Region:
    kind: str
    box: Box
    lines: tuple[Line, ...] = ()
    column: int | None = None
    order: int = 0
    ink: int = 0


@dataclass(frozen=True)
class Inventory:
    regions: tuple[Region, ...]
    gaps: tuple[Box, ...]
    columns: int
    width: int
    height: int
    ink_assigned: int
    ink_unassigned: int

    @property
    def text_regions(self) -> tuple[Region, ...]:
        """What the readers read: chrome is masked, everything else is read."""
        return tuple(region for region in self.regions if region.kind != "chrome")

    @property
    def order(self) -> tuple[int, ...]:
        return tuple(region.order for region in self.regions)


def _ink(grey: np.ndarray, level: int) -> np.ndarray:
    return grey < level


def _boxes(labels: np.ndarray, minimum: int) -> list[tuple[Box, int, int]]:
    """Component boxes, sizes and label ids, ignoring dust."""
    out: list[tuple[Box, int, int]] = []
    for index, size in enumerate(np.bincount(labels.ravel())):
        if index == 0 or size < minimum:
            continue
        rows, cols = np.where(labels == index)
        out.append(
            ((float(cols.min()), float(rows.min()), float(cols.max() + 1), float(rows.max() + 1)), int(size), index)
        )
    return out


def _row_bands(mask: np.ndarray, min_gap: int) -> list[tuple[int, int]]:
    """Text lines: rows with ink, split where the white between them is real."""
    profile = mask.sum(axis=1) > 0
    bands: list[tuple[int, int]] = []
    start: int | None = None
    gap = 0
    for y, filled in enumerate(profile):
        if filled:
            if start is None:
                start = y
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                bands.append((start, y - gap + 1))
                start = None
    if start is not None:
        bands.append((start, len(profile)))
    return bands


def _gutter(mask: np.ndarray, min_corridor: int, min_side_share: float = 0.15) -> int | None:
    """The x of a vertical corridor with no ink: where two columns meet.

    A single-column page has text across the middle, so no qualifying corridor
    exists and the page stays one column (design 7.1: detected, not forced). A
    corridor only counts with real ink on *both* sides of it, because a sparse
    page is mostly white and every x in it looks quiet.
    """
    height, width = mask.shape
    column_ink = mask.sum(axis=0)
    total = int(column_ink.sum())
    if total == 0:
        return None
    quiet = column_ink <= max(1, int(0.002 * height))

    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for x, is_quiet in enumerate(quiet):
        if is_quiet and run_start is None:
            run_start = x
        elif not is_quiet and run_start is not None:
            runs.append((run_start, x))
            run_start = None
    if run_start is not None:
        runs.append((run_start, width))

    best: tuple[int, int] | None = None
    for begin, finish in runs:
        if finish - begin < min_corridor:
            continue
        middle = (begin + finish) / 2
        if middle < 0.15 * width or middle > 0.85 * width:
            continue  # a margin is not a gutter
        left_ink = int(column_ink[:begin].sum())
        right_ink = int(column_ink[finish:].sum())
        if left_ink < min_side_share * total or right_ink < min_side_share * total:
            continue  # one column of text and a great deal of white
        if best is None or finish - begin > best[1] - best[0]:
            best = (begin, finish)
    return int((best[0] + best[1]) / 2) if best else None


def overlaps_box(a: Box, b: Box) -> float:
    """The fraction of ``a`` that ``b`` covers."""
    width = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return (width * height) / max(1.0, (a[2] - a[0]) * (a[3] - a[1]))


def _line_segments(mask: np.ndarray, band: tuple[int, int], min_gap: int) -> list[tuple[int, int]]:
    """The horizontal runs of ink inside one text line."""
    strip = mask[band[0] : band[1], :]
    profile = strip.sum(axis=0) > 0
    segments: list[tuple[int, int]] = []
    start: int | None = None
    gap = 0
    for x, filled in enumerate(profile):
        if filled:
            if start is None:
                start = x
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                segments.append((start, x - gap + 1))
                start = None
    if start is not None:
        segments.append((start, len(profile)))
    return segments


def _blue_box(rgb: np.ndarray, ink: np.ndarray) -> np.ndarray:
    """Link boxes: blue, and blue enough that grey text cannot be mistaken for it."""
    if rgb.shape[2] < 3:
        return np.zeros_like(ink)
    red, green, blue = rgb[:, :, 0].astype(int), rgb[:, :, 1].astype(int), rgb[:, :, 2].astype(int)
    return (blue > red + BLUE_LEAD) & (blue > green + BLUE_LEAD) & (blue > 120)


def inventory(
    image,
    *,
    ink_level: int = INK_LEVEL,
    min_pixels: int = MIN_COMPONENT_PIXELS,
    blue_lead: int = BLUE_LEAD,
) -> Inventory:
    """Account for every mark on a screen.

    Returns typed regions in reading order plus the gaps: the ink that could not
    be placed. On a clean synthetic page there is none; a blob dropped into a
    margin has nowhere to go but into `gaps`, which is exactly what must reach the
    assessment.
    """
    rgb = np.asarray(image.convert("RGB"))
    grey = np.asarray(image.convert("L"))
    ink = _ink(grey, ink_level)
    height, width = ink.shape
    structure = np.ones((3, 3), dtype=int)
    labels, _count = ndimage.label(ink, structure=structure)
    components = _boxes(labels, min_pixels)

    blue = _blue_box(rgb, ink) & ink

    line_gap = max(2, int(0.004 * height))
    corridor = max(8, int(0.012 * width))
    regions: list[Region] = []
    assigned = np.zeros_like(ink)

    # The reader's own furniture lives in the outer margins: a running head, a
    # page number, the scrubber bar. It is ink, and it is never text.
    chrome_components: set[int] = set()
    for box, _size, index in components:
        _x0, y0, _x1, y1 = box
        centre = (y0 + y1) / 2
        in_margin = centre <= CHROME_MARGIN * height or centre >= (1 - CHROME_MARGIN) * height
        # Furniture is a line of small text. A big solid mark in the margin is not
        # furniture, and calling it chrome would hide ink that must be reported.
        thin = (y1 - y0) <= FURNITURE_HEIGHT * height
        if in_margin and thin:
            chrome_components.add(index)

    body = ink & ~np.isin(labels, list(chrome_components)) if chrome_components else ink
    bands = _row_bands(body, line_gap)
    median_height = float(np.median([b[1] - b[0] for b in bands])) if bands else 1.0
    tolerance = max(2.0, median_height / 2)

    # The gutter is decided on the *text* rows only: a running head or a footer
    # line crosses the middle of a two-column page and would hide the corridor.
    text_only = np.zeros_like(body)
    for band in bands:
        text_only[band[0] : band[1], :] = body[band[0] : band[1], :]
    gutter = _gutter(text_only, corridor)
    columns = 2 if gutter is not None else 1

    # A panel drawn *over* the text is a popup, and a popup is chrome. A drawing
    # beside the text is a figure, and figures are read.
    band_segments = [
        segment
        for band in bands
        for segment in _line_segments(text_only, band, max(3, int(0.01 * width)))
    ]
    if band_segments:
        text_area = (
            float(min(s[0] for s in band_segments)),
            float(min(b[0] for b in bands)),
            float(max(s[1] for s in band_segments)),
            float(max(b[1] for b in bands)),
        )
        for box, _size, index in components:
            if index in chrome_components:
                continue
            x0, y0, x1, y1 = box
            big = (x1 - x0) >= 0.25 * width and (y1 - y0) >= 3 * median_height
            if big and overlaps_box(box, text_area) > 0.2:
                chrome_components.add(index)

    for box, size, index in components:
        if index in chrome_components:
            regions.append(Region(kind="chrome", box=box, column=None, order=-1, ink=size))
            assigned[labels == index] = True
    if chrome_components:
        body = ink & ~np.isin(labels, list(chrome_components))
        bands = _row_bands(body, line_gap)
        median_height = (
            float(np.median([b[1] - b[0] for b in bands])) if bands else median_height
        )
        tolerance = max(2.0, median_height / 2)

    # A line is placed by its own extent, so it can be read where it sits.
    bands = _row_bands(body, line_gap)
    placed: list[Region] = []
    for band in bands:
        segments = _line_segments(body, band, max(3, int(0.01 * width)))
        if not segments:
            continue
        y0, y1 = float(band[0]), float(band[1])
        ink_in_band = int(body[band[0] : band[1], :].sum())
        # Text is sparse inside its own box: a blob, a rule or a filled shape is
        # not, and it must reach the leftover pass where it can become a gap.
        if (y1 - y0) >= 1.5 * median_height:
            for start, end in segments:
                area = max(1.0, (end - start) * (y1 - y0))
                density = int(body[band[0] : band[1], start:end].sum()) / area
                if density >= 0.6:
                    segments = [s for s in segments if s != (start, end)]
            if not segments:
                continue
        crosses_gutter = gutter is not None and any(s[0] < gutter < s[1] for s in segments)
        taller = (y1 - y0) >= 1.2 * median_height
        first_left = (
            gutter is not None
            and taller
            and not placed
            and segments[0][0] < gutter
            and segments[-1][1] < gutter
        )
        if crosses_gutter or first_left:
            placed.append(
                Region(
                    kind="spanning_heading",
                    box=(float(segments[0][0]), y0, float(segments[-1][1]), y1),
                    lines=(Line(box=(float(segments[0][0]), y0, float(segments[-1][1]), y1), ink=ink_in_band),),
                    column=None,
                    ink=ink_in_band,
                )
            )
            continue
        for start, end in segments:
            column = 0 if gutter is None or (start + end) / 2 < gutter else 1
            placed.append(
                Region(
                    kind="text_column",
                    box=(float(start), y0, float(end), y1),
                    lines=(Line(box=(float(start), y0, float(end), y1), ink=ink_in_band),),
                    column=column,
                    ink=ink_in_band,
                )
            )

    # Merge consecutive lines of one column into blocks, and call a block a table
    # when its rows are made of widely separated runs - a tabular row, not prose.
    placed.sort(key=lambda region: (region.column if region.column is not None else -1, region.box[1], region.box[0]))
    blocks: list[Region] = []
    for region in placed:
        if (
            blocks
            and region.kind == "text_column"
            and blocks[-1].kind == "text_column"
            and blocks[-1].column == region.column
            and region.box[1] - blocks[-1].box[3] <= 1.5 * median_height
        ):
            previous = blocks[-1]
            blocks[-1] = Region(
                kind="text_column",
                box=(min(previous.box[0], region.box[0]), previous.box[1], max(previous.box[2], region.box[2]), region.box[3]),
                lines=previous.lines + region.lines,
                column=previous.column,
                ink=previous.ink + region.ink,
            )
        else:
            blocks.append(region)

    for block in blocks:
        kind = block.kind
        if kind == "text_column" and len(block.lines) >= 3:
            table_rows = 0
            for line in block.lines:
                segments = _line_segments(body, (int(line.box[1]), int(line.box[3])), max(3, int(0.025 * width)))
                if len(segments) >= 3:
                    table_rows += 1
            if table_rows >= 3:
                kind = "table"
        regions.append(
            Region(kind=kind, box=block.box, lines=block.lines, column=block.column, ink=block.ink)
        )
        x0, y0, x1, y1 = (int(v) for v in block.box)
        assigned[y0:y1, x0:x1] |= body[y0:y1, x0:x1]

    # Blue boxes are note links, and they are found before the leftover pass so a
    # link inside a text line is not read as prose.
    blue_components: set[int] = set()
    for box, size, index in components:
        if index in chrome_components:
            continue
        x0, y0, x1, y1 = (int(v) for v in box)
        # Tested on the component's own pixels. Comparing label ids from two
        # labellings would be comparing two unrelated numbering schemes, which is
        # how a blue note link went missing.
        blue_fraction = int(blue[y0:y1, x0:x1].sum()) / max(1, size)
        if blue_fraction < 0.5:
            continue
        blue_components.add(index)
        # Where a note link lives is inside a line of prose, which is exactly why
        # it cannot be skipped for being already covered.
        regions.append(Region(kind="note_link", box=box, column=None, ink=size))
        assigned[y0:y1, x0:x1] |= ink[y0:y1, x0:x1]

    # Whatever ink is left: a figure if it is a solid body of marks, a gap if it
    # is anything else. Never silently dropped.
    gaps: list[Box] = []
    for box, _size, index in components:
        if index in chrome_components or index in blue_components:
            continue
        x0, y0, x1, y1 = (int(v) for v in box)
        leftovers = int((ink[y0:y1, x0:x1] & ~assigned[y0:y1, x0:x1]).sum())
        if leftovers < min_pixels:
            continue
        area = max(1, (x1 - x0) * (y1 - y0))
        solid = leftovers / area
        if (y1 - y0) >= 4 * median_height and solid >= 0.25:
            regions.append(Region(kind="figure", box=box, column=None, ink=leftovers))
            assigned[y0:y1, x0:x1] |= ink[y0:y1, x0:x1]
        else:
            # A gap is ink nobody could place. It stays unassigned: that count is
            # the measure of how much of the page this pass failed to account for.
            gaps.append(box)

    # Reading order: top to bottom, columns left to right, a spanning heading in
    # place - so a heading between two column blocks is read where it sits.
    ordered = sorted(
        [r for r in regions if r.kind != "chrome"],
        key=lambda region: (round(region.box[1] / tolerance), region.box[0]),
    )
    with_order = []
    for index, region in enumerate(ordered):
        with_order.append(
            Region(
                kind=region.kind,
                box=region.box,
                lines=region.lines,
                column=region.column,
                order=index,
                ink=region.ink,
            )
        )
    chromes = [r for r in regions if r.kind == "chrome"]
    final = tuple(with_order) + tuple(chromes)

    placed_ink = int(assigned[ink].sum())
    total_ink = int(ink.sum())
    return Inventory(
        regions=final,
        gaps=tuple(gaps),
        columns=columns,
        width=width,
        height=height,
        ink_assigned=placed_ink,
        ink_unassigned=max(0, total_ink - placed_ink),
    )
