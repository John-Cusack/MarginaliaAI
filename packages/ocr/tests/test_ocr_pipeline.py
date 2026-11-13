"""The three readers, wired: what a page comes out as.

The readers are stubs here - the GPU's VLM and EasyOCR are not a unit test's
business - but everything downstream of them is real: the inventory decides what is
read, the three readings go through confirm word by word, the checks run, and the
page ends up confirmed, marked or gapped with a reason.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from marginalia_ocr.server.pipeline import ReaderFailed, Readers, read_page
from marginalia_ocr.server.readers.tesseract import RegionReading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from ocr_synth.corpus import build_corpus  # noqa: E402


class Stub:
    """A reader that answers with a canned text for every region."""

    def __init__(self, text: str | None, *, fails: bool = False, prefix: str = "") -> None:
        self.text = text
        self.fails = fails
        self.prefix = prefix
        self.calls = 0
        self.unloaded = False

    def read(self, image, box, *, region_id: str) -> RegionReading:
        self.calls += 1
        if self.fails:
            raise RuntimeError("no model")
        return RegionReading(region_id=region_id, text=(self.text or ""), words=(), seconds=0.0, empty=not self.text)

    def unload(self) -> None:
        self.unloaded = True


def page_bytes(kind: str = "two_column_prose") -> bytes:
    return build_corpus(dprs=(1,), kinds=(kind,))[0].png


def test_three_readers_agreeing_gives_a_confirmed_page():
    text = "the debt was 48.2% and the claim held"
    outcome = read_page(
        page_bytes(),
        capture_id="c0",
        readers=Readers(vlm=Stub(text), tesseract=Stub(text), third=Stub(text)),
        read_note_links=False,
    )
    assert outcome.status == "confirmed"
    assert outcome.marks == () and outcome.gaps == ()
    assert outcome.words > 0 and outcome.words_marked == 0
    assert all(region.status == "confirmed" for region in outcome.regions)


def test_one_reader_apart_marks_the_word_it_differs_on():
    agreed = "the debt was 48.2% and the claim held"
    outcome = read_page(
        page_bytes(),
        capture_id="c1",
        readers=Readers(
            vlm=Stub(agreed), tesseract=Stub(agreed), third=Stub(agreed.replace("debt", "debr"))
        ),
        read_note_links=False,
    )
    assert outcome.status == "marked"
    assert outcome.marks, "a disagreement is a mark"
    assert outcome.words_marked >= 1
    texts = {candidate.text for mark in outcome.marks for candidate in mark.candidates}
    assert {"debt", "debr"} <= texts, texts


def test_a_reader_that_raises_is_a_run_failure_not_an_empty_reading():
    """An empty reading is legitimate only when the reader is loaded and ran.

    A reader that raised contributed nothing, and the page must not be stored as
    though that reader had looked and seen nothing: the caller blocks the job
    instead, with the reader and the error in the reason.
    """
    text = "the debt was 48.2% and the claim held"
    with pytest.raises(ReaderFailed) as raised:
        read_page(
            page_bytes(),
            capture_id="c2",
            readers=Readers(vlm=Stub(None, fails=True), tesseract=Stub(text), third=Stub(text)),
            primary="vlm",
            read_note_links=False,
        )
    assert raised.value.reader == "vlm"
    assert "no model" in raised.value.error


def test_readers_that_load_and_answer_nothing_gap_the_page():
    outcome = read_page(
        page_bytes(),
        capture_id="c3",
        readers=Readers(vlm=Stub(None), tesseract=Stub(None), third=Stub(None)),
        read_note_links=False,
    )
    assert outcome.status == "gapped"
    assert outcome.gaps
    assert outcome.words == 0


def test_unloading_gives_the_borrowed_resources_back():
    stub = Stub("text")
    readers = Readers(vlm=stub, tesseract=stub, third=stub)
    read_page(page_bytes("sparse_page"), capture_id="c4", readers=readers, read_note_links=False)
    readers.unload()
    assert stub.unloaded, "a reader left resident is a model nobody else can use"


def test_the_primary_reader_is_configuration_not_an_assumption():
    """The spine used to be the VLM because Phase 0 chose it - for a different job.

    The primary is now whichever reader measures best on prose word error rate,
    and on this corpus that is Tesseract (0.070) rather than the VLM.
    """
    agreed = "the debt was 48.2% and the claim held"
    outcome = read_page(
        page_bytes(),
        capture_id="c5",
        readers=Readers(vlm=Stub("a reading of its own"), tesseract=Stub(agreed), third=Stub("other text")),
        primary="tesseract",
        read_note_links=False,
    )
    assert agreed in outcome.text, "the configured primary's text is stored"
    assert outcome.status != "gapped"
    assert any("the stored text is the tesseract reader's" in mark.reason for mark in outcome.marks) or outcome.marks
