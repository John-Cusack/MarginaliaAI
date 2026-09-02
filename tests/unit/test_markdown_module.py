"""Markdown parsing: what survives into the canonical text, and what that buys.

The module used to delete `#` markers on the way in, along with the emphasis and
link syntax it was meant to clean. That is not a cosmetic difference. The
canonical text is the only thing every later pass reads — the node tree, the
search window, `reindex structure` — so a heading dropped here is gone for good,
recoverable only by parsing the source file again. One document in the corpus was
stored that way: 39 headings, none of them in the text.

It also declared `structural` as its chunker and then handed over no section
table, and the pipeline reads a missing table as "this format has no structure"
rather than "this parser forgot to say" — so it quietly got prose windows.
"""

from __future__ import annotations

import pytest

from research_engine.domain.nodes import build_node_tree
from research_engine.modules.markdown import MarkdownModule
from research_engine.services.ingestion.pipeline import run_chunking
from research_engine.services.ingestion.structure import node_tree_for

BOOK = """\
# The Whole Thing

Opening prose before any section.

## Part One

Body of part one, which runs for a while.

### A Subsection

Detail under part one, with **bold** and a [link](https://example.com).

## Part Two

Body of part two.
"""


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "book.md"
    path.write_text(BOOK, encoding="utf-8")
    return path


async def parse(path):
    return await MarkdownModule().parse(path)


class TestHeadingsSurvive:
    async def test_the_markers_reach_the_canonical_text(self, book):
        text, _title, _meta = await parse(book)

        assert "# The Whole Thing" in text
        assert "### A Subsection" in text

    async def test_formatting_that_is_only_formatting_is_still_stripped(self, book):
        """The fix is about headings, not about giving up on stripping."""
        text, _title, _meta = await parse(book)

        assert "**bold**" not in text and "bold" in text
        assert "](https://example.com)" not in text and "a link" in text

    async def test_a_document_with_no_headings_yields_no_sections(self, tmp_path):
        path = tmp_path / "flat.md"
        path.write_text("Just prose.\n\nMore prose.\n", encoding="utf-8")

        text, title, meta = await parse(path)

        assert meta["sections"] == []
        assert meta["heading_count"] == 0
        assert title == "flat"  # the stem, there being no heading to take
        assert text.startswith("Just prose.")


class TestSectionTable:
    async def test_every_section_slices_back_to_its_own_heading(self, book):
        """The contract passages keep, owed by the table that positions them."""
        text, _title, meta = await parse(book)

        for section in meta["sections"]:
            span = text[section["char_start"] : section["char_end"]]
            assert span.lstrip().startswith("#")
            assert section["heading"] in span.splitlines()[0]

    async def test_levels_come_from_the_marker_depth(self, book):
        _text, _title, meta = await parse(book)

        assert [s["level"] for s in meta["sections"]] == [1, 2, 3, 2]

    async def test_a_heading_inside_a_code_fence_is_not_a_section(self, tmp_path):
        """Why `heading_count` counts the table rather than the raw file.

        The fence is stripped before the headings are read, so a shell comment
        cannot open a chapter. Counting the raw text instead would report a
        heading the section table does not have, and the two must agree.
        """
        path = tmp_path / "fenced.md"
        path.write_text(
            "# Real Heading\n\nProse.\n\n```sh\n# not a heading\n```\n\nMore.\n",
            encoding="utf-8",
        )

        _text, _title, meta = await parse(path)

        assert meta["heading_count"] == 1
        assert [s["heading"] for s in meta["sections"]] == ["Real Heading"]


class TestWhatTheTableUnlocks:
    async def test_the_declared_chunker_is_the_one_that_runs(self, book):
        """`default_chunker()` said structural and the pipeline gave prose
        windows, because a missing section table is indistinguishable from a
        format with no structure. Nothing about the passages looked wrong."""
        text, _title, meta = await parse(book)

        drafts = await run_chunking(text, MarkdownModule().default_chunker(), meta)

        headings = {d.metadata.get("section_heading") for d in drafts}
        assert "A Subsection" in headings

    async def test_the_document_gets_a_nested_tree_not_a_single_root(self, book):
        text, title, meta = await parse(book)

        nodes = build_node_tree(meta["sections"], text_length=len(text), title=title)

        assert len(nodes) > 1
        assert max(n.depth for n in nodes) >= 2, "subsection did not nest"

    async def test_reindex_rebuilds_the_same_shape_from_stored_text_alone(self, book):
        """The other half of the fix.

        `reindex structure` re-reads the canonical text and never sees the
        parser's table, so headings surviving into the text is what makes a
        markdown document repairable after the fact rather than only at ingest.
        """
        text, title, meta = await parse(book)

        from_parser = build_node_tree(
            meta["sections"], text_length=len(text), title=title
        )
        from_text = node_tree_for(text, title=title)

        assert [(n.title, n.depth) for n in from_text] == [
            (n.title, n.depth) for n in from_parser
        ]
