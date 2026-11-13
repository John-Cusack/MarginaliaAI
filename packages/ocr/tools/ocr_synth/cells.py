"""Tables as cells, for the audit mode that reads a grid instead of a page.

A table's canonical text is Markdown, and no reader produces Markdown: scoring a
reader against pipes measures the reader's markdown, not its reading. The audit's
per-book sample cannot drop table screens either (design §9.4 stratifies on
them), so tables are audited the way they are read — cell by cell. Claude gets
the screen *and* the candidate grid as plain cell text with row and column
labels, and reports differences by cell.

This module supplies the two halves of that: the labelled text a cell grid is
rendered as, and a seeded set planted inside cells, so the mode's sensitivity can
be measured like any other auditor's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ocr_synth.seeded_errors import NoMaterial, plant

if TYPE_CHECKING:
    import random
    from collections.abc import Iterable

    from ocr_synth.layout import Region


def cell_grid_text(cells: Iterable[tuple[str, str]]) -> str:
    """The candidate text for a table: one labelled line per cell, no pipes.

    Row and column numbers are 1-based, the way the prompt refers to them.
    """
    return "\n".join(
        f"row {row} col {col}: {text}" for (row, col), text in sorted(cells)
    )


def table_cells(regions: Iterable[Region]) -> tuple[tuple[int, int, str], ...]:
    """Every cell of every table region, as (row, column, text), 1-based."""
    found: list[tuple[int, int, str]] = []
    for region in regions:
        if region.kind != "table":
            continue
        for row_index, row in enumerate(region.cells, start=1):
            for col_index, text in enumerate(row, start=1):
                found.append((row_index, col_index, text))
    return tuple(found)


def edit_cell(text: str, rng: random.Random) -> str | None:
    """One plausible error inside a cell: a digit changed, or a letter swapped.

    Table cells are mostly numbers and short labels, so the page planter often has
    no material to work with. This is the cell mode's own predeclared edit, and
    the class of error a table actually suffers: a digit that is wrong, or a word
    whose letters are wrong.
    """
    digits = [index for index, character in enumerate(text) if character.isdigit()]
    if digits:
        index = rng.choice(digits)
        current = int(text[index])
        replacement = (current + rng.choice((1, 2, 3, 4, 6, 7, 8, 9))) % 10
        return text[:index] + str(replacement) + text[index + 1 :]
    letters = [
        index for index, character in enumerate(text) if character.isalpha() and index > 0
    ]
    if letters:
        index = rng.choice(letters)
        return text[:index] + ("a" if text[index].lower() != "a" else "e") + text[index + 1 :]
    return None


def plant_cells(
    cells: tuple[tuple[int, int, str], ...],
    *,
    per_table: int = 3,
    rng: random.Random,
) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Plant errors inside cells; return the grid text and the planted cells.

    Detection in this mode is per cell — an auditor that reports a difference in
    that cell found it — so the planted set is a set of cells, not of spans. The
    planters are the page ones, so the same kinds turn up: a real-word
    substitution, a dropped word, a digit changed.
    """
    by_cell: dict[tuple[int, int], str] = {(row, col): text for row, col, text in cells}
    planted: list[tuple[int, int]] = []
    candidates = [(row, col, text) for row, col, text in cells if text.strip()]
    rng.shuffle(candidates)
    for row, col, text in candidates:
        if len(planted) >= per_table:
            break
        # Prefer the page planters where a cell can carry them (a multi-word
        # label can), and fall back to the cell's own edit (a number or a word).
        edited: str | None = None
        try:
            seeded = plant(text, rng, {"real_word": 1, "omission": 1})
            if seeded.planted:
                edited = seeded.final
        except NoMaterial:
            edited = None
        if edited is None:
            edited = edit_cell(text, rng)
        if edited is None or edited == text:
            continue
        by_cell[(row, col)] = edited
        planted.append((row, col))
    grid = cell_grid_text(sorted(by_cell.items()))
    return grid, tuple(sorted(planted))
