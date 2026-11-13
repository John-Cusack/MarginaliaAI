"""Tesseract as a reader: psm 6, per region, with boxes and confidences.

The mapping from Tesseract's word table to page coordinates is where a reader can
quietly lie - a crop's offset dropped, a confidence of -1 read as a low score
instead of "none" - so it is a pure function with its own tests. The binary is
only needed for the integration test, which skips rather than pretends.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from marginalia_ocr.server.readers.tesseract import (
    ReaderUnavailable,
    available,
    read_region,
    words_from_data,
)
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from ocr_synth.corpus import build_corpus  # noqa: E402

NEEDS_BINARY = pytest.mark.skipif(not available(), reason="tesseract is not installed")


def test_word_boxes_are_returned_in_page_coordinates():
    """A crop's offset is not optional: a word box is meaningless on its own."""
    data = {
        "text": ["hello", "world", ""],
        "conf": ["96", "88", "-1"],
        "left": [10, 40, 0],
        "top": [5, 5, 0],
        "width": [20, 25, 0],
        "height": [12, 12, 0],
    }
    words = words_from_data(data, offset=(100.0, 200.0))
    assert [word.text for word in words] == ["hello", "world"]
    assert words[0].box == (110.0, 205.0, 130.0, 217.0)
    assert words[1].confidence == 88.0


def test_a_confidence_of_minus_one_is_no_reading_not_a_low_one():
    data = {"text": ["noise"], "conf": ["-1"], "left": [0], "top": [0], "width": [5], "height": [5]}
    assert words_from_data(data) == ()


def test_an_empty_word_table_is_an_empty_reading():
    assert words_from_data({"text": [], "conf": []}) == ()


def test_a_missing_binary_is_an_error_not_empty_text():
    """A reader that cannot run must never be mistaken for agreement."""
    with pytest.raises(ReaderUnavailable):
        read_region(Image.new("RGB", (100, 100), "white"), (0, 0, 50, 50), region_id="r0", command="tesseract-nope")


@NEEDS_BINARY
def test_a_two_column_page_reads_both_columns_in_order():
    page = build_corpus(dprs=(1,), kinds=("two_column_prose",))[0]
    import io

    image = Image.open(io.BytesIO(page.png)).convert("RGB")
    left, right = page.regions[0], page.regions[1]

    first = read_region(image, left.box, region_id="column-0")
    second = read_region(image, right.box, region_id="column-1")

    assert not first.empty and not second.empty
    assert first.region_id == "column-0" and second.region_id == "column-1"
    for reading, region in ((first, left), (second, right)):
        for word in reading.words:
            assert region.box[0] - 2 <= word.box[0] and word.box[2] <= region.box[2] + 2
            assert region.box[1] - 2 <= word.box[1] and word.box[3] <= region.box[3] + 2
            assert 0.0 <= word.confidence <= 100.0
        assert reading.seconds >= 0.0
    # the left column's text is the left column's text, not the page's
    assert first.text.split()[0] != second.text.split()[0]
    assert left.text.split()[0].casefold().strip(".,;:") in first.text.casefold()


@NEEDS_BINARY
def test_an_empty_crop_reads_as_empty_and_never_as_agreement():
    reading = read_region(Image.new("RGB", (80, 40), "white"), (0, 0, 80, 40), region_id="blank")
    assert reading.empty is True
    assert reading.text == ""
    assert reading.words == ()
