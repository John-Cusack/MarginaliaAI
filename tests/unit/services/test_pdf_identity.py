"""PDF parsers expose identifiers printed by the document itself."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from research_engine.modules.pdf_text import PDFTextModule

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


async def test_pdf_text_uses_only_the_first_page_for_identity(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "article.pdf"
    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), "Article title\nDOI: 10.1177/026537880001700202")
    second = document.new_page()
    second.insert_text((72, 72), "References\nDOI: 10.1000/cited-work")
    document.save(path)
    document.close()

    _text, _title, metadata = await PDFTextModule().parse(path)

    assert metadata["edition_key"] == "doi:10.1177/026537880001700202"
    assert metadata["doi"] == "10.1177/026537880001700202"
