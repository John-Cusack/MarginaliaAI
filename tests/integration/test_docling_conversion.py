"""Docling conversion against a real PDF.

This path had no test of any kind: Docling was never imported by the suite and
no PDF was ever converted, so every assertion about offsets, pages and chunk
seams rested on hand-rolled fakes agreeing with a library nobody exercised.

Marked `slow` rather than `integration` — it needs no Postgres, it needs a
layout model and a minute of CPU.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = [pytest.mark.slow]

PAGES = 24
_SEPARATOR = "\n\n"


@pytest.fixture(scope="module")
def pdf(tmp_path_factory) -> Iterator[Path]:
    """A multi-page PDF with headings, so there is structure to recover.

    `fitz` is already a dependency — `_pdf_page_count` and `_pdf_has_text` both
    use it — so this costs no new package.
    """
    import fitz

    path = tmp_path_factory.mktemp("docling") / "synthetic.pdf"
    doc = fitz.open()
    for number in range(1, PAGES + 1):
        page = doc.new_page()
        if number % 6 == 1:
            page.insert_text((72, 96), f"Chapter {number // 6 + 1}", fontsize=24)
        page.insert_text(
            (72, 160),
            f"Page {number} of the synthetic corpus. "
            f"It carries enough prose to be detected as body text rather than "
            f"discarded as furniture, across several lines so the layout model "
            f"has a block to find.",
            fontsize=11,
        )
    doc.save(str(path))
    doc.close()
    yield path


@pytest.fixture(scope="module")
def whole(pdf: Path):
    from research_engine.modules.docling_converter import _convert_page_range

    return _convert_page_range(str(pdf), 1, PAGES, ocr=False, device="cpu")


def test_every_section_slices_back_to_its_own_text(whole) -> None:
    """The invariant the offsets exist to keep.

    Structure used to be recovered by running a heading regex back over exported
    markdown; it is now read off the item stream with a cursor, which is exact by
    construction — but only if the cursor arithmetic is right.
    """
    text, sections, _pages = whole[:3]

    assert sections, "no structure recovered at all"
    for section in sections:
        assert 0 <= section["char_start"] < section["char_end"] <= len(text)
        assert text[section["char_start"] : section["char_end"]]


def test_page_provenance_survives(whole) -> None:
    """Page numbers were discarded entirely, leaving PDF locators at 0%."""
    _text, _sections, pages = whole[:3]

    numbers = [entry["page"] for entry in pages]
    assert numbers == sorted(numbers), "page markers out of document order"
    assert set(numbers) <= set(range(1, PAGES + 1))
    assert len(numbers) >= PAGES // 2


def test_the_worker_reports_its_own_peak_memory(whole) -> None:
    """The measurement that replaces the guess. A worker that reports nothing
    leaves `_WORKER_MEMORY_MB` unfalsifiable, which is how it drifted four times
    off the truth without anyone noticing."""
    peak_mb = whole[3]

    assert peak_mb > 0
    assert peak_mb < 64_000  # sanity: MB, not kB


@pytest.mark.parametrize("pages_per_task", [8, 12])
def test_splitting_into_tasks_does_not_change_the_canonical_text(
    pdf: Path, whole, pages_per_task: int
) -> None:
    """Whether `docling_pages_per_task` is a re-ingest or just a tuning knob.

    Task size is now an operator setting and the recovery ladder lowers it on
    its own, so if a different split produced different text, the same PDF would
    land in the corpus differently depending on how much memory the machine had
    at the time.
    """
    from research_engine.modules.docling_converter import (
        _convert_page_range,
        _join_chunks,
    )

    pieces = [
        _convert_page_range(str(pdf), start, min(start + pages_per_task - 1, PAGES),
                            ocr=False, device="cpu")
        for start in range(1, PAGES + 1, pages_per_task)
    ]
    joined, _sections, pages = _join_chunks([(t, s, p) for t, s, p, _ in pieces])

    assert joined == whole[0]
    assert [entry["page"] for entry in pages] == [
        entry["page"] for entry in whole[2]
    ]
