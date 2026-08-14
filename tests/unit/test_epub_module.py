"""EPUB parsing: reading order, apparatus exclusion, and section offsets.

The fixture below is built so that the manifest order and the spine order
disagree, because that is the case the parser used to get wrong: it iterated
the manifest, which is a bag of files in packaging order, and produced a
canonical text whose chapters were shuffled. Every passage offset addressed
against such a text points at the wrong prose, so this is checked directly
rather than through a chunker.
"""

from __future__ import annotations

import pytest

from research_engine.modules.epub import EPUBModule
from research_engine.services.ingestion.pipeline import run_chunking

CHAPTERS = [
    ("c1.xhtml", "Chapter One", "AAA first chapter body."),
    ("c2.xhtml", "Chapter Two", "BBB second chapter body."),
    ("c3.xhtml", "Chapter Three", "CCC third chapter body."),
]


@pytest.fixture
def shuffled_epub(tmp_path):
    """An EPUB whose manifest order (3, 1, 2) is not its spine order (1, 2, 3)."""
    epub = pytest.importorskip("ebooklib.epub", reason="ebooklib not installed")

    book = epub.EpubBook()
    book.set_identifier("order-test-001")
    book.set_title("Order Test")
    book.set_language("en")

    items = {}
    for index, (file_name, heading, body) in enumerate(CHAPTERS, start=1):
        chapter = epub.EpubHtml(title=heading, file_name=file_name, lang="en")
        chapter.content = f"<html><body><h1>{heading}</h1><p>{body}</p></body></html>"
        items[index] = chapter

    for index in (3, 1, 2):  # manifest order, deliberately not reading order
        book.add_item(items[index])

    book.spine = [items[1], items[2], items[3]]
    book.toc = [items[1], items[2], items[3]]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    path = tmp_path / "order_test.epub"
    epub.write_epub(str(path), book)
    return path


async def test_canonical_text_follows_the_spine(shuffled_epub):
    text, _title, _metadata = await EPUBModule().parse(shuffled_epub)

    assert text.index("AAA") < text.index("BBB") < text.index("CCC")


async def test_navigation_document_is_not_ingested_as_prose(shuffled_epub):
    text, _title, metadata = await EPUBModule().parse(shuffled_epub)

    # The nav document lists every chapter title. If it were ingested, each
    # heading would appear twice and there would be a fourth "chapter".
    assert metadata["chapter_count"] == len(CHAPTERS)
    assert text.count("Chapter Three") == 1


async def test_sections_address_the_canonical_text(shuffled_epub):
    text, _title, metadata = await EPUBModule().parse(shuffled_epub)

    sections = metadata["sections"]
    assert len(sections) == len(CHAPTERS)
    assert [section["heading"] for section in sections] == [
        heading for _file, heading, _body in CHAPTERS
    ]
    for section in sections:
        span = text[section["char_start"] : section["char_end"]]
        assert span.startswith(section["heading"])
        # Boundaries only: carrying the prose would store it a second time.
        assert "text" not in section


async def test_structural_chunker_runs_and_spans_are_exact(shuffled_epub):
    module = EPUBModule()
    text, _title, metadata = await module.parse(shuffled_epub)

    drafts = await run_chunking(text, module.default_chunker(), metadata)

    assert [draft.chunker for draft in drafts] == ["structural"] * len(CHAPTERS)
    for draft in drafts:
        assert text[draft.char_start : draft.char_end] == draft.text
        assert draft.locator["heading"]
        # The document-wide section table must not be copied onto every passage.
        assert "sections" not in draft.metadata


async def test_plain_text_still_falls_back_to_prose_windows():
    """A parser that supplies no sections must not break structural ingestion."""
    drafts = await run_chunking("Some prose with no structure at all.", "structural")

    assert drafts
    assert drafts[0].chunker == "prose_window"
