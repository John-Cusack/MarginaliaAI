"""The table audit's own mode: a grid of cells, not Markdown, and its own scoring.

A table's canonical text is Markdown. No reader writes Markdown, so measuring one
against pipes measures the wrong thing; the per-book sample cannot drop table
screens either. What this mode must get right, and what this file pins: the
candidate is labelled cell text without pipes, the planted errors are inside
cells, and a detection is a difference reported *in that cell*.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import claude_cell_checker as checker  # noqa: E402
from ocr_synth import cells as cellmod  # noqa: E402
from ocr_synth.corpus import build_corpus  # noqa: E402


def table_screen():
    return build_corpus(dprs=(1,), kinds=("table_page",))[0]


def test_cells_are_labelled_from_one_and_carry_no_pipes():
    screen = table_screen()
    cells = cellmod.table_cells(screen.regions)
    assert cells, "the table page has cells"
    grid = cellmod.cell_grid_text(sorted({(row, col): text for row, col, text in cells}.items()))
    assert grid.splitlines()[0].startswith("row 1 col 1: ")
    assert "|" not in grid and "---" not in grid


def test_a_cell_edit_changes_a_digit_or_a_letter():
    import random as _random

    assert cellmod.edit_cell("1990", _random.Random(1)) != "1990"
    assert cellmod.edit_cell("Debt", _random.Random(1)) != "Debt"
    assert cellmod.edit_cell("", _random.Random(1)) is None


def test_planted_errors_sit_inside_cells_and_are_reported_by_cell():
    screen = table_screen()
    cells = cellmod.table_cells(screen.regions)
    plain = cellmod.cell_grid_text(sorted({(row, col): text for row, col, text in cells}.items()))
    rng = random.Random(3)
    grid, planted = cellmod.plant_cells(cells, per_table=3, rng=rng)

    assert len(planted) == 3, "three cells carry a planted error"
    assert set(planted) <= {(row, col) for row, col, _ in cells}
    assert grid != plain, "the planted grid differs from the clean one"
    for row, _col in planted:
        assert f"row {row} col " in grid


def test_planting_is_deterministic_for_a_seed():
    screen = table_screen()
    cells = cellmod.table_cells(screen.regions)
    first = cellmod.plant_cells(cells, per_table=3, rng=random.Random(11))
    second = cellmod.plant_cells(cells, per_table=3, rng=random.Random(11))
    assert first == second


def test_a_cell_the_planting_left_alone_is_not_in_the_planted_set():
    screen = table_screen()
    cells = cellmod.table_cells(screen.regions)
    grid, planted = cellmod.plant_cells(cells, per_table=1, rng=random.Random(5))
    untouched = [cell for cell in cells if (cell[0], cell[1]) not in planted]
    assert untouched
    for row, col, text in untouched:
        assert f"row {row} col {col}: {text}" in grid


def test_validate_requires_cells_and_bounded_snippets():
    good = {"differences": [{"row": 1, "col": 2, "snippet": "Debt (% GDP)", "reading": "Debt (5 GDP)"}]}
    assert checker.validate(good)[0].row == 1

    with pytest.raises(checker.ResponseRejected):
        checker.validate({"differences": [{"row": 0, "col": 1, "snippet": "a", "reading": "b"}]})
    with pytest.raises(checker.ResponseRejected):
        checker.validate({"differences": [{"row": 1, "col": 1, "snippet": " ".join(["w"] * 9), "reading": "b"}]})
    with pytest.raises(checker.ResponseRejected):
        checker.validate({"differences": [{"row": 1, "snippet": "a", "reading": "b"}]})
    with pytest.raises(checker.ResponseRejected):
        checker.validate({"nope": []})


def test_a_failed_call_is_a_rejection_not_a_score(monkeypatch, tmp_path):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(checker.subprocess, "run", lambda *a, **k: Result())
    result = checker.check_cells(tmp_path / "t.png", "row 1 col 1: x")
    assert result.rejected == "exit 1" and result.differences == ()


def test_a_good_answer_is_parsed_with_its_cost_and_word_volume(monkeypatch, tmp_path):
    payload = {"differences": [{"row": 2, "col": 3, "snippet": "1990 12.4", "reading": "1990 42.4"}]}
    stdout = json.dumps({"result": payload, "total_cost_usd": 0.02, "model": "test"})

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self):
            self.stdout = stdout

    monkeypatch.setattr(checker.subprocess, "run", lambda *a, **k: Result())
    result = checker.check_cells(tmp_path / "t.png", "row 1 col 1: x")
    assert result.differences[0] == checker.CellDifference(2, 3, "1990 12.4", "1990 42.4", None)
    assert result.cost_usd == 0.02
