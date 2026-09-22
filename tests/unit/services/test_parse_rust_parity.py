"""The Rust parser seam must be byte-identical to the Python it replaces.

Every vector runs under both ``RE_RUST_BACKEND=rust`` and ``=python``;
``(text, title, metadata)`` triples and ``(score, reason)`` detections must
compare equal. Reads and decoding stay caller-side, so bad bytes raise
``UnicodeDecodeError`` on either backend; heads cross as newline-normalized
bytes.

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import pytest

from research_engine.modules.markdown import MarkdownModule
from research_engine.modules.plain_text import PlainTextModule

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

TEXTS = [
    "A Title Here\n\nBody text one.\n",
    "ünïcodé tître\n\nBodÿ with émojis 😀.\n",
    "x" * 300 + "\n\nBody.\n",
    "short.",
    "   \n\t  \n",
    "",
    "line one\r\nline two\r\n\r\nline three\r\n",
]


def _write(tmp_path, name, content: bytes):
    path = tmp_path / name
    path.write_bytes(content)
    return path


class TestPlainTextParity:
    @pytest.mark.parametrize("text", TEXTS)
    async def test_parse_matches_across_backends(self, text, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, "n.txt", text.encode("utf-8"))
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await PlainTextModule().parse(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await PlainTextModule().parse(path)
        assert actual == expected

    async def test_bad_bytes_raise_decode_errors_either_way(self, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, "n.txt", b"good start \xff\xfe bad tail")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(UnicodeDecodeError):
            await PlainTextModule().parse(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(UnicodeDecodeError):
            await PlainTextModule().parse(path)

    @pytest.mark.parametrize(
        "name, content, score",
        [
            ("n.txt", b"hello", 0.8),
            ("n.md", b"hello", 0.3),
            ("noext", b"hello world", 0.3),
            ("noext", b"\xff\xfe\x00", 0.0),
            ("noext", b"", 0.3),
        ],
    )
    async def test_detect_matches_across_backends(self, name, content, score, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, name, content)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await PlainTextModule().detect(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await PlainTextModule().detect(path)
        assert actual == expected
        assert actual[0] == score

    async def test_python_path_needs_no_wheel(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        path = _write(tmp_path, "n.txt", b"Title\n\nBody.\n")
        assert await PlainTextModule().parse(path) == (
            "Title\n\nBody.\n",
            "Title",
            {"char_count": 13, "line_count": 3, "file_name": "n.txt"},
        )


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
        ],
    )
    async def test_parse_matches_across_backends(self, text, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, "b.md", text.encode("utf-8"))
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await MarkdownModule().parse(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await MarkdownModule().parse(path)
        assert actual == expected
        assert actual[2]["sections"] == expected[2]["sections"]

    async def test_book_sections_keep_shape(self, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
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

    @pytest.mark.parametrize(
        "name, content, score",
        [
            ("b.md", b"# Hi\n", 0.9),
            ("b.txt", b"# Hi\n", 0.4),
            ("noext", b"# Heading here\n", 0.4),
            ("noext", b"plain prose", 0.0),
            ("noext", b"\xff\xfe", 0.0),
        ],
    )
    async def test_detect_matches_across_backends(self, name, content, score, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, name, content)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await MarkdownModule().detect(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await MarkdownModule().detect(path)
        assert actual == expected
        assert actual[0] == score
