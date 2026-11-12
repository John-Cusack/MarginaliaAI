"""A reader's loop is a gap, never text.

The VLM returned 7,923 characters of one repeated sentence on a 1x region crop.
Stored as text that is worse than nothing: it is embedded, quotable, and carries
a citation. These tests pin the guard that turns it into a gap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ocr_synth.text import PROSE
from reader_guards import (
    Guarded,
    guard_reading,
    longest_repeat_run,
    most_common_block,
    region_capacity,
    repetition_density,
)

# Real prose from the corpus, varied: the guard must never fire on this.
CLEAN = "\n\n".join(PROSE)
LOOP = "is not a detail: a list of revisions read " * 200


def test_a_clean_reading_is_text():
    result = guard_reading(CLEAN, box=(0, 0, 552, 600), font_size=16)
    assert isinstance(result, Guarded)
    assert result.gap is False and result.text == CLEAN.strip()


def test_a_repetition_loop_is_a_gap():
    """The real 1x loop has a nine-word period, so it is caught by the count."""
    result = guard_reading(LOOP, box=(0, 0, 552, 600), font_size=16)
    assert result.gap is True
    assert result.text == ""
    assert "repetition" in result.reason
    assert most_common_block(LOOP) > 100


def test_prose_that_repeats_a_phrase_a_few_times_is_still_text():
    once = "a list of revisions read " + CLEAN
    twice = "a list of revisions read " * 2 + CLEAN
    assert guard_reading(once).gap is False
    assert guard_reading(twice).gap is False


def test_a_verbatim_block_that_is_most_of_the_reading_is_a_gap():
    """A refrained line inside prose is fine; a reading that is mostly one block
    is not — which is what the nine-word loop of the 1x VLM looks like."""
    many = "a list of revisions read. " * 30
    assert most_common_block(many) >= 6
    assert guard_reading(many).gap is True
    few = "a list of revisions read. " * 8
    assert guard_reading(few + "\n\n" + CLEAN).gap is False, (
        "the same block inside a page of prose is not most of the reading"
    )


def test_a_repeated_refrain_inside_real_prose_is_not_a_gap():
    refrained = ("And the margin holds. " * 3) + CLEAN
    assert guard_reading(refrained).gap is False


def test_consecutive_repeats_are_caught_at_three():
    thrice = "one two three four five six seven eight " * 3
    assert longest_repeat_run(thrice) == 3
    assert guard_reading(thrice).gap is True


def test_an_empty_reading_is_a_gap():
    assert guard_reading("   ").gap is True
    assert guard_reading("", ).reason == "empty reading"


def test_a_reading_far_beyond_its_region_capacity_is_a_gap():
    box = (0.0, 0.0, 552.0, 600.0)
    capacity = region_capacity(box, 16.0)
    assert capacity is not None and 1000 < capacity < 3000
    varied = " ".join(f"token{index}" for index in range(300))
    assert guard_reading(varied, box=box, font_size=16.0).gap is False
    over = guard_reading(" ".join(f"token{index}" for index in range(4000)), box=box, font_size=16.0)
    assert over.gap is True and "where the region holds about" in over.reason


def test_without_a_region_size_repetition_is_still_guarded():
    """A loop is a loop even when nothing tells the guard how big the region was."""
    assert guard_reading("word " * 4000).gap is True
    assert guard_reading(" ".join(f"word {index}" for index in range(4000))).gap is False


def test_the_capacity_guard_needs_a_font_size():
    assert region_capacity((0.0, 0.0, 552.0, 600.0), None) is None
    assert region_capacity((0.0, 0.0, 0.0, 600.0), 16.0) is None


def test_repetition_density_finds_the_period_of_a_loop():
    count, window, coverage = repetition_density("a list of revisions read. " * 30)
    assert count >= 6 and coverage >= 0.5, (count, window, coverage)
    _, _, prose = repetition_density(CLEAN)
    assert prose < 0.2, "varied prose must score far below the threshold"


def test_repeat_run_counts_consecutive_blocks_only():
    assert longest_repeat_run("alpha beta gamma delta epsilon zeta eta theta") == 1
    assert longest_repeat_run("one two three four five six seven eight " * 4) == 4


def test_the_real_1x_loop_and_the_real_3x_reading_are_judged_differently():
    """The captured readings, if this machine still has them."""
    one_x = Path("/tmp/readings_regions_1x.json")
    three_x = Path("/tmp/readings_regions_3x.json")
    if not one_x.exists() or not three_x.exists():
        pytest.skip("the VLM region readings are not on this machine")
    loops = json.loads(one_x.read_text())
    clean = json.loads(three_x.read_text())
    worst = max(loops, key=lambda key: len(loops[key]))
    same_region = worst.replace("1x", "3x")
    assert guard_reading(loops[worst]).gap is True, "the 1x loop must be guarded"
    assert guard_reading(clean[same_region]).gap is False, "the 3x reading must survive"
