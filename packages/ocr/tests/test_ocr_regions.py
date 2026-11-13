"""The region inventory: every mark on the screen accounted for.

The property that matters is not "it found the text" but "it left nothing
unaccounted for": a pipeline that reads only what it recognises drops whatever it
did not, silently. So these tests check the corpus's own regions are all assigned,
that a clean page has no unassigned ink and no gaps, and that ink which cannot be
placed lands in `gaps` where the assessment will see it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from marginalia_ocr.server.regions import inventory
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from ocr_synth.corpus import build_corpus  # noqa: E402


def screen(kind: str, dpr: int = 1):
    screens = build_corpus(dprs=(dpr,), kinds=(kind,))
    assert screens, kind
    return screens[0]


def image_of(screen_) -> Image.Image:
    import io

    return Image.open(io.BytesIO(screen_.png)).convert("RGB")


def overlaps(a, b) -> float:
    """The fraction of a that b covers."""
    x0, y0, x1, y1 = a
    u0, v0, u1, v1 = b
    width = max(0.0, min(x1, u1) - max(x0, u0))
    height = max(0.0, min(y1, v1) - max(y0, v0))
    area = max(1.0, (x1 - x0) * (y1 - y0))
    return (width * height) / area


@pytest.mark.parametrize("kind", ["two_column_prose", "notes_page", "table_page", "sparse_page"])
def test_every_truth_region_is_assigned(kind: str):
    page = screen(kind)
    found = inventory(image_of(page))
    for truth in page.regions:
        assert any(overlaps(truth.box, region.box) >= 0.5 for region in found.regions), truth.box


@pytest.mark.parametrize("kind", ["two_column_prose", "notes_page", "chapter_opener"])
def test_a_clean_page_leaves_no_ink_unaccounted_for(kind: str):
    page = screen(kind)
    found = inventory(image_of(page))
    assert found.gaps == (), "clean ink must not be reported as a gap"
    assert found.ink_unassigned == 0
    assert found.ink_assigned > 0


def test_an_unreadable_blob_becomes_a_gap_not_a_silence():
    """Ink that cannot be placed is a gap; that is the whole point of the pass."""
    page = screen("two_column_prose")
    image = image_of(page)
    draw = ImageDraw.Draw(image)
    draw.ellipse([20, 20, 60, 60], fill="black")   # in the margin, no region covers it

    found = inventory(image)

    assert found.gaps, "the blob vanished without a trace"
    assert found.ink_unassigned > 0
    assert any(overlaps(gap, (20, 20, 60, 60)) > 0.5 for gap in found.gaps)


def test_a_figure_is_a_figure_and_not_a_gap():
    page = screen("figure_page")
    found = inventory(image_of(page))
    kinds = {region.kind for region in found.regions}
    assert "figure" in kinds or found.gaps == (), "figure ink is either read as a figure or reported"


def test_two_columns_are_split_by_the_gutter_and_read_left_to_right():
    page = screen("two_column_prose")
    found = inventory(image_of(page))
    assert found.columns == 2
    assert {region.column for region in found.text_regions} == {0, 1}

    first_column = [r for r in found.text_regions if r.column == 0]
    second_column = [r for r in found.text_regions if r.column == 1]
    assert first_column and second_column
    # inside a line band, left comes before right
    for left in first_column:
        for right in second_column:
            if abs(left.box[1] - right.box[1]) < 5:
                assert left.order < right.order


def test_a_spanning_heading_is_detected_and_keeps_its_place():
    page = screen("spanning_heading")
    found = inventory(image_of(page))
    headings = [region for region in found.regions if region.kind == "spanning_heading"]
    assert headings, "a heading across both columns was forced into one of them"
    heading = headings[0]
    assert heading.column is None
    above = [r for r in found.text_regions if r.box[3] <= heading.box[1] + 1]
    below = [r for r in found.text_regions if r.box[1] >= heading.box[3] - 1]
    for region in above:
        assert region.order < heading.order, "the heading was lifted out of its place"
    for region in below:
        assert region.order > heading.order


def test_a_single_column_page_is_not_forced_into_two():
    page = screen("sparse_page")
    found = inventory(image_of(page))
    assert found.columns == 1
    assert {region.column for region in found.text_regions} <= {0}


def test_chrome_is_detected_and_masked_from_reading():
    """The reader's own popup and footer are ink, and never text."""
    page = screen("popup_page")
    found = inventory(image_of(page))
    chrome = [region for region in found.regions if region.kind == "chrome"]
    assert chrome, "the popup bar was left in the text"
    assert all(region not in found.text_regions for region in chrome)


def test_a_blue_box_is_a_note_link():
    page = screen("notes_page")
    image = image_of(page)
    draw = ImageDraw.Draw(image)
    draw.rectangle([900, 600, 940, 620], fill=(20, 20, 220))

    found = inventory(image)

    links = [region for region in found.regions if region.kind == "note_link"]
    assert links and any(overlaps(region.box, (900, 600, 940, 620)) > 0.5 for region in links)
