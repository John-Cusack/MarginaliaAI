"""HTML and TEI: the section table their declared chunker always needed.

Both modules returned `"structural"` from `default_chunker()` and then handed
the pipeline no `metadata["sections"]`. `run_chunking` reads a missing table as
"this format has no structure" rather than "this parser forgot to say", so both
silently got prose windows — correct for plain text, wrong for a format built
out of headings. Nothing about the resulting passages looked wrong.

TEI carried two further defects that only showed up once its text was read
closely: nested divs had their prose stored twice, and adjacent blocks were
concatenated with no separator at all.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from research_engine.domain.nodes import build_node_tree
from research_engine.modules.html import HTMLModule, _text_and_sections
from research_engine.modules.tei_xml import TEIXMLModule
from research_engine.services.ingestion.pipeline import run_chunking

HTML = """<!doctype html><html lang="en"><head><title>A Page</title></head><body>
<!-- a comment, which get_text() excludes -->
<h1>Top Heading</h1><p>Intro paragraph.</p>
<h2>First <em>Nested</em> Section</h2><p>Body of first.</p>
<script>var x = 1;</script>
<h2>Second Section</h2><ul><li>alpha</li><li>beta</li></ul>
</body></html>"""

TEI = """<?xml version="1.0"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt>
<title>A Treatise</title><author>Some Author</author></titleStmt></fileDesc></teiHeader>
<text><body>
<div><head>Part One</head><p>Intro to part one.</p>
  <div><head>Chapter A</head><p>Alpha body.</p></div>
  <div><head>Chapter B</head><p>Beta body.</p></div>
</div>
</body></text></TEI>"""


@pytest.fixture
def html_file(tmp_path):
    path = tmp_path / "page.html"
    path.write_text(HTML, encoding="utf-8")
    return path


@pytest.fixture
def tei_file(tmp_path):
    path = tmp_path / "doc.xml"
    path.write_text(TEI, encoding="utf-8")
    return path


class TestHtmlTextIsUnchanged:
    """The offsets are only worth something if they address the stored string,
    and leaving that string alone is what makes this a pure addition."""

    def test_it_reproduces_get_text_exactly(self):
        soup = BeautifulSoup(HTML, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        body = soup.find("body")

        text, _sections = _text_and_sections(body)

        assert text == body.get_text(separator="\n", strip=True)

    def test_comments_are_not_text(self):
        """`Comment` subclasses `NavigableString`, so an isinstance check would
        have pulled the comment into the document."""
        soup = BeautifulSoup(HTML, "html.parser")

        text, _ = _text_and_sections(soup.find("body"))

        assert "a comment" not in text


class TestHtmlSections:
    async def test_every_section_starts_at_its_own_heading(self, html_file):
        text, _title, meta = await HTMLModule().parse(html_file)

        assert meta["sections"]
        for section in meta["sections"]:
            span = text[section["char_start"] : section["char_end"]]
            assert " ".join(span.split()).startswith(section["heading"])

    async def test_the_label_normalises_whitespace_the_text_keeps(self, html_file):
        """`<h2>First <em>Nested</em> Section</h2>` is three strings, so the
        canonical text joins them with the separator `get_text` uses while the
        heading reads as one line. A breadcrumb holding a newline looks broken,
        and moving the text to match would change every stored offset."""
        text, _title, meta = await HTMLModule().parse(html_file)
        section = meta["sections"][1]

        assert section["heading"] == "First Nested Section"
        assert text[section["char_start"] : section["char_end"]].startswith(
            "First\nNested\nSection"
        )

    async def test_levels_come_from_the_tag_number(self, html_file):
        _text, _title, meta = await HTMLModule().parse(html_file)

        assert [s["level"] for s in meta["sections"]] == [1, 2, 2]

    async def test_markup_inside_a_heading_is_flattened(self, html_file):
        _text, _title, meta = await HTMLModule().parse(html_file)

        assert meta["sections"][1]["heading"] == "First Nested Section"

    async def test_a_page_with_no_headings_yields_no_sections(self, tmp_path):
        path = tmp_path / "flat.html"
        path.write_text("<html><body><p>Just prose.</p></body></html>", encoding="utf-8")

        text, _title, meta = await HTMLModule().parse(path)

        assert meta["sections"] == []
        assert text == "Just prose."


class TestTeiDuplication:
    """`.//div` matches at every depth and `itertext()` already descends, so a
    parent stored each child's prose a second time."""

    async def test_a_nested_chapter_is_stored_once(self, tei_file):
        text, _title, _meta = await TEIXMLModule().parse(tei_file)

        assert text.count("Alpha body.") == 1
        assert text.count("Beta body.") == 1

    async def test_blocks_are_separated_rather_than_welded(self, tei_file):
        """`"".join(el.itertext())` produced `Chapter AAlpha body.` — a word on
        neither side of the seam, embedded and searched as one."""
        text, _title, _meta = await TEIXMLModule().parse(tei_file)

        assert "AAlpha" not in text
        assert "OneIntro" not in text
        assert "Chapter A\nAlpha body." in text


class TestTeiSections:
    async def test_nesting_depth_becomes_the_level(self, tei_file):
        _text, _title, meta = await TEIXMLModule().parse(tei_file)

        assert [(s["heading"], s["level"]) for s in meta["sections"]] == [
            ("Part One", 1),
            ("Chapter A", 2),
            ("Chapter B", 2),
        ]

    async def test_a_parent_section_stops_at_its_first_child(self, tei_file):
        """Sections are disjoint; `build_node_tree` widens parents from `level`."""
        text, _title, meta = await TEIXMLModule().parse(tei_file)
        part, chapter_a = meta["sections"][0], meta["sections"][1]

        assert part["char_end"] <= chapter_a["char_start"]
        assert text[part["char_start"] : part["char_end"]] == "Part One\nIntro to part one."

    async def test_a_body_with_no_divs_still_produces_text(self, tmp_path):
        path = tmp_path / "flat.xml"
        path.write_text(
            '<?xml version="1.0"?><TEI xmlns="http://www.tei-c.org/ns/1.0">'
            "<teiHeader/><text><body><p>Only prose here.</p></body></text></TEI>",
            encoding="utf-8",
        )

        text, _title, meta = await TEIXMLModule().parse(path)

        assert text == "Only prose here."
        assert meta["sections"] == []


@pytest.mark.parametrize("which", ["html", "tei"])
class TestTheChunkerTheyDeclare:
    async def _parsed(self, which, html_file, tei_file):
        module = HTMLModule() if which == "html" else TEIXMLModule()
        source = html_file if which == "html" else tei_file
        text, title, meta = await module.parse(source)
        return module, text, title, meta

    async def test_structural_chunking_actually_runs(self, which, html_file, tei_file):
        module, text, _title, meta = await self._parsed(which, html_file, tei_file)

        drafts = await run_chunking(text, module.default_chunker(), meta)

        assert any(d.metadata.get("section_heading") for d in drafts)

    async def test_offsets_round_trip(self, which, html_file, tei_file):
        module, text, _title, meta = await self._parsed(which, html_file, tei_file)

        drafts = await run_chunking(text, module.default_chunker(), meta)

        for draft in drafts:
            assert text[draft.char_start : draft.char_end] == draft.text

    async def test_the_tree_nests(self, which, html_file, tei_file):
        _module, text, title, meta = await self._parsed(which, html_file, tei_file)

        nodes = build_node_tree(meta["sections"], text_length=len(text), title=title)

        assert max(n.depth for n in nodes) >= 2
