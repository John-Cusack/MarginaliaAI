"""Tests for DoclingModule detection, chunker, and document type."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from research_engine.modules.docling_converter import DoclingModule


@pytest.fixture
def module() -> DoclingModule:
    return DoclingModule()


class TestDetect:
    """Detection confidence for various file extensions."""

    @pytest.mark.parametrize("ext", [".pdf", ".docx", ".pptx", ".xlsx"])
    async def test_high_confidence_formats(self, module: DoclingModule, ext: str) -> None:
        path = PurePosixPath(f"/tmp/test{ext}")
        confidence, reason = await module.detect(path)
        assert confidence == 0.95
        assert ext in reason

    @pytest.mark.parametrize("ext", [".html", ".htm", ".md", ".csv", ".png", ".jpg", ".tiff", ".tex"])
    async def test_medium_confidence_formats(self, module: DoclingModule, ext: str) -> None:
        path = PurePosixPath(f"/tmp/test{ext}")
        confidence, reason = await module.detect(path)
        assert confidence == 0.85
        assert ext in reason

    @pytest.mark.parametrize("ext", [".txt", ".epub", ".xml", ".json", ".odt", ".rtf"])
    async def test_unsupported_formats(self, module: DoclingModule, ext: str) -> None:
        path = PurePosixPath(f"/tmp/test{ext}")
        confidence, _reason = await module.detect(path)
        assert confidence == 0.0


class TestDefaults:
    """Default chunker and document type."""

    def test_default_chunker(self, module: DoclingModule) -> None:
        assert module.default_chunker() == "structural"

    def test_default_document_type(self, module: DoclingModule) -> None:
        assert module.default_document_type() == "generic"

    @pytest.mark.parametrize(
        ("ext", "expected"),
        [
            (".xlsx", "spreadsheet"),
            (".csv", "spreadsheet"),
            (".pptx", "presentation"),
            (".pdf", "generic"),
            (".docx", "generic"),
            (".html", "generic"),
        ],
    )
    def test_document_type_for_extension(self, module: DoclingModule, ext: str, expected: str) -> None:
        path = PurePosixPath(f"/tmp/file{ext}")
        assert module.default_document_type_for(path) == expected


class TestModuleAttributes:
    def test_id(self, module: DoclingModule) -> None:
        assert module.id == "docling"

    def test_version(self, module: DoclingModule) -> None:
        assert module.version == "2.0"


# --- structure from the item stream -----------------------------------------


class FakeProv:
    def __init__(self, page_no: int) -> None:
        self.page_no = page_no


class FakeItem:
    """Enough of a Docling item for the builder: label, level, prov, markdown."""

    def __init__(self, text, label="text", level=None, page=None, markdown=None):
        self.text = text
        self.label = label
        self.level = level
        self.prov = [FakeProv(page)] if page else []
        self._markdown = markdown if markdown is not None else text

    def export_to_markdown(self, _doc):
        return self._markdown


class FakeDoc:
    def __init__(self, items):
        self._items = items

    def iterate_items(self):
        return ((item, 0) for item in self._items)


class TestTextAndStructure:
    """Offsets exact by construction, not recovered by a regex.

    The previous approach exported markdown and ran a heading regex back over
    it. Docling writes every heading as `##`, so that saw one level however deep
    the document went, and never saw a page number at all — PDF locators sat at
    0% across the corpus.
    """

    ITEMS = [
        FakeItem("Chapter One", label="section_header", level=1, page=3,
                 markdown="## Chapter One"),
        FakeItem("Opening prose about the campaign.", page=3),
        FakeItem("More prose, now overleaf.", page=4),
        FakeItem("A Digression", label="section_header", level=2, page=5,
                 markdown="### A Digression"),
        FakeItem("Digressive prose.", page=5),
    ]

    def _built(self):
        from research_engine.modules.docling_converter import _text_and_structure

        return _text_and_structure(FakeDoc(self.ITEMS))

    def test_every_section_slices_back_out_of_the_text(self):
        text, sections, _ = self._built()

        for section in sections:
            assert text[section["char_start"] : section["char_end"]]

    def test_sections_come_from_headings_not_from_every_item(self):
        """A section per item would make a 600-page book forty thousand nodes."""
        _, sections, _ = self._built()

        assert [s["heading"] for s in sections] == ["Chapter One", "A Digression"]

    def test_levels_come_from_the_item_not_from_counting_hashes(self):
        _, sections, _ = self._built()

        assert [s["level"] for s in sections] == [1, 2]

    def test_a_section_runs_to_the_next_heading_not_just_its_own_title(self):
        text, sections, _ = self._built()

        assert sections[0]["char_end"] == sections[1]["char_start"]
        assert sections[-1]["char_end"] == len(text)

    def test_page_boundaries_are_recorded_where_the_page_changes(self):
        text, _, pages = self._built()

        assert [p["page"] for p in pages] == [3, 4, 5]
        for boundary in pages:
            assert 0 <= boundary["char_start"] <= len(text)

    def test_a_section_carries_the_page_it_starts_on(self):
        _, sections, _ = self._built()

        assert [s["page"] for s in sections] == [3, 5]

    def test_an_item_with_no_provenance_does_not_invent_a_page(self):
        from research_engine.modules.docling_converter import _text_and_structure

        _, _, pages = _text_and_structure(FakeDoc([FakeItem("no page here")]))

        assert pages == []


class TestJoinChunks:
    """Worker results carry offsets relative to their own page range."""

    def test_offsets_are_shifted_onto_the_whole_document(self):
        from research_engine.modules.docling_converter import _join_chunks

        first = ("AAAA", [{"char_start": 0, "char_end": 4, "heading": "A"}],
                 [{"char_start": 0, "page": 1}])
        second = ("BBBB", [{"char_start": 0, "char_end": 4, "heading": "B"}],
                  [{"char_start": 0, "page": 9}])

        text, sections, pages = _join_chunks([first, second])

        assert text == "AAAA\n\nBBBB"
        assert sections[1]["char_start"] == 6
        assert text[sections[1]["char_start"] : sections[1]["char_end"]] == "BBBB"

    def test_page_numbers_are_absolute_and_are_not_shifted(self):
        """Docling numbers pages absolutely, so only offsets move."""
        from research_engine.modules.docling_converter import _join_chunks

        _, _, pages = _join_chunks([
            ("AAAA", [], [{"char_start": 0, "page": 1}]),
            ("BBBB", [], [{"char_start": 0, "page": 9}]),
        ])

        assert [p["page"] for p in pages] == [1, 9]
        assert [p["char_start"] for p in pages] == [0, 6]

    def test_a_section_ending_at_a_chunk_edge_runs_on_into_the_next(self):
        """Otherwise the prose between two workers belongs to no section."""
        from research_engine.modules.docling_converter import _join_chunks

        _, sections, _ = _join_chunks([
            ("AAAA", [{"char_start": 0, "char_end": 4, "heading": "A"}], []),
            ("BBBB", [{"char_start": 2, "char_end": 4, "heading": "B"}], []),
        ])

        assert sections[0]["char_end"] == sections[1]["char_start"]
