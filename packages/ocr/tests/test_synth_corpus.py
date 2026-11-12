"""The synthetic corpus: shape, truth, ink placement and layout classes.

These tests are the corpus's own contract. They are what makes a Phase 0 number
mean something: if the truth text, the region boxes and the drawn pixels can
drift apart, every measurement taken against them is fiction.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from ocr_synth import text as T
from ocr_synth.corpus import Screen, build_corpus
from ocr_synth.layout import PAGE_H, PAGE_W
from PIL import Image, ImageChops, ImageDraw

# A pixel darker than this counts as ink. Region boxes are tight glyph boxes, so
# the paper allowance is only for the antialiasing at their edge.
INK_THRESHOLD = 200
PAPER_TOLERANCE = 1

EXPECTED_KINDS = {
    "chapter_opener",
    "two_column_prose",
    "spanning_heading",
    "notes_page",
    "toc_page",
    "location_page",
    "table_page",
    "table_continuation",
    "two_column_notes",
    "figure_page",
    "sparse_page",
    "popup_page",
    "split_word_page",
}


@pytest.fixture(scope="module")
def clean() -> tuple[Screen, ...]:
    return build_corpus(dprs=(1,))


def only(screens: tuple[Screen, ...], kind: str, dpr: int = 1) -> Screen:
    matches = [screen for screen in screens if screen.kind == kind and screen.dpr == dpr]
    assert len(matches) == 1, f"expected one {kind}@{dpr}x, got {len(matches)}"
    return matches[0]


def image_of(screen: Screen) -> Image.Image:
    return Image.open(BytesIO(screen.png)).convert("RGB")


def ink_mask(image: Image.Image) -> Image.Image:
    return image.convert("L").point(lambda value: 255 if value < INK_THRESHOLD else 0)


def coverage_mask(screen: Screen, dpr: int, tolerance: int = PAPER_TOLERANCE) -> Image.Image:
    mask = Image.new("L", (PAGE_W * dpr, PAGE_H * dpr), 0)
    draw = ImageDraw.Draw(mask)
    for region in screen.regions + screen.chrome:
        x0, y0, x1, y1 = region.box
        draw.rectangle(
            [
                max(0, x0 * dpr - tolerance),
                max(0, y0 * dpr - tolerance),
                min(PAGE_W * dpr, x1 * dpr + tolerance),
                min(PAGE_H * dpr, y1 * dpr + tolerance),
            ],
            fill=255,
        )
    return mask


def overlaps(first, second) -> bool:
    return (
        first[0] < second[2] and second[0] < first[2] and first[1] < second[3] and second[1] < first[3]
    )


def test_corpus_covers_every_layout_class(clean):
    assert {screen.kind for screen in clean} == EXPECTED_KINDS
    for screen in clean:
        assert screen.png and screen.truth_text and screen.regions


def test_png_dimensions_match_the_requested_dpr():
    screens = build_corpus(dprs=(1, 3), kinds=("two_column_prose",))
    one = only(screens, "two_column_prose", 1)
    three = only(screens, "two_column_prose", 3)
    assert image_of(one).size == (PAGE_W, PAGE_H)
    assert image_of(three).size == (PAGE_W * 3, PAGE_H * 3)
    assert one.truth_text == three.truth_text
    assert one.image_sha256 != three.image_sha256
    assert one.frame_geometry() == three.frame_geometry()


def test_all_ink_is_inside_a_declared_region(clean):
    for screen in clean:
        outside = ImageChops.subtract(ink_mask(image_of(screen)), coverage_mask(screen, 1))
        assert outside.getbbox() is None, f"{screen.screen_id}: ink outside region boxes"


def test_readable_regions_are_disjoint(clean):
    for screen in clean:
        boxes = [region.box for region in screen.regions]
        for index, first in enumerate(boxes):
            for second in boxes[index + 1 :]:
                assert not overlaps(first, second), f"{screen.screen_id}: {first} overlaps {second}"


def test_two_column_reading_order_is_left_then_right(clean):
    screen = only(clean, "two_column_prose")
    left, right = screen.regions
    assert left.kind == right.kind == "text_column"
    assert left.box[2] < right.box[0], "the gutter must separate the columns"
    assert left.line_count > 5 and right.line_count > 0


def test_spans_partition_the_truth_text(clean):
    for screen in clean:
        assert [span.region_index for span in screen.spans] == list(range(len(screen.regions)))
        cursor = 0
        for span in screen.spans:
            assert span.char_start == cursor, screen.screen_id
            assert screen.truth_text[span.char_start : span.char_end] == screen.regions[
                span.region_index
            ].text
            cursor = span.char_end + 2
        assert cursor - 2 == len(screen.truth_text)


def test_layout_lines_are_1x_and_inside_the_page(clean):
    for screen in clean:
        assert screen.layout_lines
        for x0, y0, x1, y1 in screen.layout_lines:
            assert 0 <= x0 < x1 <= PAGE_W, screen.screen_id
            assert 0 < y0 < y1 <= PAGE_H, screen.screen_id


def test_screen_record_carries_binding_data_and_no_text(clean):
    record = only(clean, "two_column_prose").record()
    assert set(record) == {
        "screen_id",
        "kind",
        "dpr",
        "width",
        "height",
        "image_sha256",
        "page_label",
        "label_kind",
        "scrubber",
        "page_data",
        "layout_lines",
    }
    assert record["page_data"]["start"] < record["page_data"]["end"]
    assert record["scrubber"] == record["page_data"]["start"]


def test_chrome_is_declared_and_never_in_reading_order(clean):
    for screen in clean:
        assert "footer" in {region.detail for region in screen.chrome}
        for region in screen.chrome:
            assert region.text and region.text not in screen.truth_text, screen.screen_id


def test_page_labels_cover_arabic_roman_and_location(clean):
    assert {screen.label_kind for screen in clean} == {"arabic", "roman", "location"}
    assert only(clean, "location_page").page_label == T.FOOTER_LOCATION_TEMPLATE.format(
        location=1234, total=9999
    )
    assert only(clean, "chapter_opener").page_label == "Page vi of 368"
    assert only(clean, "two_column_prose").page_label == "Page 42 of 368"


def test_popup_page_declares_the_popup_as_chrome(clean):
    screen = only(clean, "popup_page")
    popups = [region for region in screen.chrome if region.detail == "popup"]
    assert len(popups) == 1
    assert popups[0].text == T.POPUP_TEMPLATE.format(location=312)
    assert all(region.detail != "popup" for region in clean[0].chrome)


def test_note_links_are_blue_and_address_their_text(clean):
    screen = only(clean, "notes_page")
    subs = [sub for region in screen.regions for sub in region.sub]
    assert len(subs) == 2
    image = image_of(screen)
    for region in screen.regions:
        for sub in region.sub:
            assert sub.kind == "note_link"
            token = region.text[sub.char_start : sub.char_end]
            assert token in {"[AR2]", "[AR7]"}
            crop = image.crop(sub.box)
            blue = sum(
                count
                for count, (red, green, blue_value) in crop.getcolors(maxcolors=1 << 20)
                if blue_value - red > 60 and blue_value > 100
            )
            assert blue > 0, f"no blue ink in {sub}"


def test_split_word_page_breaks_a_word_with_a_hyphen(clean):
    screen = only(clean, "split_word_page")
    lines = [line.text for region in screen.regions for line in region.lines]
    assert any(line.endswith("transfor-") for line in lines), lines
    assert T.SPLIT_WORD in screen.truth_text
    assert "transfor-" not in screen.truth_text


def test_table_pages_carry_cells_and_canonical_markdown(clean):
    screen = only(clean, "table_page")
    table = next(region for region in screen.regions if region.kind == "table")
    assert table.cells[0] == T.TABLE_HEADER
    assert table.cells[1] == T.TABLE_ROWS[0]
    assert len(table.cells) == len(T.TABLE_ROWS) + 1
    assert table.grid is not None and table.caption in screen.truth_text
    assert "| Year | Debt (% GDP) | Revision (pp) | Notes |" in screen.truth_text

    continuation = only(clean, "table_continuation")
    cont = next(region for region in continuation.regions if region.kind == "table")
    assert cont.detail == "continuation"
    assert cont.cells[0] == table.cells[0]
    assert cont.cells[1] == T.TABLE_ROWS_CONTINUATION[0]


def test_blur_changes_pixels_without_moving_geometry():
    sharp = build_corpus(dprs=(1,), kinds=("two_column_prose",))
    blurred = build_corpus(dprs=(1,), kinds=("two_column_prose",), blur=0.4)
    assert only(sharp, "two_column_prose").image_sha256 != only(blurred, "two_column_prose").image_sha256
    assert only(sharp, "two_column_prose").frame_geometry() == only(
        blurred, "two_column_prose"
    ).frame_geometry()
