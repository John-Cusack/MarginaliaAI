"""The degraded-ink variant, and why it is the only source of pipeline errors.

On crisp synthetic prose three readers agree on nothing wrong: the pipeline-shaped
population — mistakes all readers make, which is what a three-reader confirm step
would let through — comes out empty, so the pipeline's own error rate has no
denominator. Degraded ink is the case where the same wrong reading is the easiest
one for every engine, and this file pins the properties that make it usable: the
truth does not move, the ink really does change, and the record says what it was.
"""

from __future__ import annotations

import io

from ocr_synth.corpus import build_corpus
from ocr_synth.degrade import Degrade
from PIL import Image

KINDS = ("two_column_prose",)
DEGRADED = Degrade(small_caps=True, light=True, kern=-0.8, blur=0.3)


def _mean_luminance(png: bytes) -> float:
    with Image.open(io.BytesIO(png)) as image:
        histogram = image.convert("L").histogram()
    total = sum(histogram)
    return sum(index * count for index, count in enumerate(histogram)) / total


def test_degradation_changes_the_ink_and_not_the_truth():
    clean = build_corpus(dprs=(1,), kinds=KINDS, font=None)[0]
    degraded = build_corpus(dprs=(1,), kinds=KINDS, font=None, degrade=DEGRADED)[0]

    assert degraded.truth_text == clean.truth_text, "the canonical text must not move"
    assert degraded.spans == clean.spans, "the spans texts are anchored to must not move"
    assert degraded.frame_geometry() == clean.frame_geometry(), "geometry must not move"
    assert degraded.image_sha256 != clean.image_sha256, "the ink must actually change"
    assert _mean_luminance(degraded.png) > _mean_luminance(clean.png), "light ink is lighter"


def test_the_record_says_what_was_applied():
    degraded = build_corpus(dprs=(1,), kinds=KINDS, degrade=DEGRADED)[0]
    assert degraded.degrade is not None
    assert degraded.degrade["small_caps"] is True
    assert degraded.degrade["light"] is True
    assert degraded.degrade["kern"] == -0.8
    assert degraded.screen_id.endswith(f"+{DEGRADED.label}")

    clean = build_corpus(dprs=(1,), kinds=KINDS)[0]
    assert clean.degrade is None
    assert "+" not in clean.screen_id.split("@")[-1]


def test_small_caps_draw_capitals_and_keep_the_text_case():
    # The page shows capitals; the text keeps its own case, which is what makes a
    # reader that answers MARGIN for Margin wrong against the text and right
    # about the page. Both halves of that are the variant's whole point.
    drawn, size = DEGRADED.drawn("Margin", 20)
    assert drawn == "MARGIN"
    assert size < 20
    assert DEGRADED.drawn("Margin", 20)[0] != "Margin"
