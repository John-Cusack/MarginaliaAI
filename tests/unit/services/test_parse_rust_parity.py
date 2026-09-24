"""The Rust markdown parser must be byte-identical to the Python it replaces.

Every vector runs under both ``RE_RUST_BACKEND=rust`` and ``=python``;
``(text, title, metadata)`` triples must compare equal. The read stays
caller-side. Only ``parse`` crosses: plain-text parsing and every content
detection stay Python (they lost the accelerator benchmark's keep gate).

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import pytest

from research_engine.modules.markdown import MarkdownModule

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

def _write(tmp_path, name, content: bytes):
    path = tmp_path / name
    path.write_bytes(content)
    return path


class TestMarkdownParity:
    @pytest.mark.parametrize(
        "text",
        [
            BOOK,
            "No headings here, just prose. " * 50,
            "",
            "# Lone heading with no body\n",
            "```\n# not a heading\n```\n\nReal text.\n",
            "Heading\r\n\r\nBody with\r\nCRLF line breaks.\r\n",
            # Horizontal rule with trailing space: Python's greedy pattern.
            "a\n--- \n \nb",
            "***\n\ntext\n\n___  \n",
            # Heading-dense (a glossary): section offsets over many headings.
            "".join(f"## Term {i}\n\nDefinition {i}, with é and 😀.\n\n" for i in range(3000)),
        ],
    )
    async def test_parse_matches_across_backends(self, text, tmp_path, monkeypatch):
        pytest.importorskip("research_engine._native")
        path = _write(tmp_path, "b.md", text.encode("utf-8"))
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await MarkdownModule().parse(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await MarkdownModule().parse(path)
        assert actual == expected
        assert actual[2]["sections"] == expected[2]["sections"]

    async def test_book_sections_keep_shape(self, tmp_path, monkeypatch):
        pytest.importorskip("research_engine._native")
        path = _write(tmp_path, "b.md", BOOK.encode("utf-8"))
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        text, title, meta = await MarkdownModule().parse(path)
        assert title == "The Whole Thing"
        assert meta["heading_count"] == len(meta["sections"]) == 4
        assert meta["sections"][0] == {
            "char_start": 0,
            "char_end": 52,
            "heading": "The Whole Thing",
            "level": 1,
        }

    async def test_python_path_needs_no_wheel(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "research_engine._native", raising=False)
        monkeypatch.setitem(sys.modules, "research_engine._native", None)
        path = _write(tmp_path, "b.md", BOOK.encode("utf-8"))
        _, title, meta = await MarkdownModule().parse(path)
        assert title == "The Whole Thing"
        assert meta["heading_count"] == 4
