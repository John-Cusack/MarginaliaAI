"""The Rust format parsers must be byte-identical to the Python modules.

Every vector runs under both ``RE_RUST_BACKEND=rust`` and ``=python``;
``(text, title, metadata)`` triples and ``(score, reason)`` detections must
compare equal. Raw bytes cross (replace-mode decoding and ZIP/XML handling
live in the crate); reads stay caller-side. Corrupt inputs raise on either
backend (``ValueError`` with the crate's message on Rust, engine-native
errors on Python — callers catch ``Exception``). The missing-dependency
gates stay on the Python path only: the Rust backend parses without
``bs4``/``ebooklib``/``lxml``.

One documented residual: ``&#[0-9]+[a-f]`` without a semicolon corrupts
html.parser's tag state (following markup leaks as text — version-fragile,
probed on beautifulsoup4 4.x); the crate decodes per the missing-semicolon
rule instead. Pinned explicitly below: if a BeautifulSoup upgrade fixes
the leak, that test fails to signal full equality may hold.

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from research_engine.modules.epub import EPUBModule
from research_engine.modules.html import HTMLModule
from research_engine.modules.tei_xml import TEIXMLModule

HTML_DOC = (
    "<html><head><title>T</title>"
    '<meta name="author" content="A U Thor">'
    '<meta name="description" content="About things.">'
    "</head><body><h1>H</h1><p>First &#65; para with <a href=\"x\">link</a>.</p>"
    "<h2>Sub</h2><p>Second para with \u00e9mojis \U0001f600.</p></body></html>"
)

TEI_DOC = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><titleStmt>'
    "<title>Work</title><author>Auth</author></titleStmt>"
    '<publicationStmt><date when="2020-01-01">2020</date></publicationStmt>'
    "</teiHeader><text><body><div><head>Part</head><p>Prose one.</p>"
    "<p>Prose two with caf\u00e9.</p></div>"
    "<div><p>Other part.</p><note>A note.</note></div>"
    "</body></text></TEI>"
)


def _write(tmp_path, name, content: bytes):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _epub_bytes(chapters, title="T", language="en"):
    """A minimal EPUB from stdlib zipfile alone (no ebooklib needed)."""
    container = (
        '<?xml version="1.0"?><container '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        "<rootfiles><rootfile "
        'media-type="application/oebps-package+xml" '
        'full-path="EPUB/content.opf"/></rootfiles></container>'
    )
    files = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": container,
    }
    manifest, spine = [], []
    for index, (href, heading, body) in enumerate(chapters):
        manifest.append(
            f'<item href="{href}" id="c{index}" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="c{index}"/>')
        files[f"EPUB/{href}"] = (
            f"<html><body><h1>{heading}</h1><p>{body}</p></body></html>"
        )
    files["EPUB/content.opf"] = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf">'
        f"<metadata><dc:title xmlns:dc=\"http://purl.org/dc/elements/1.1/\">{title}</dc:title>"
        f"<dc:language xmlns:dc=\"http://purl.org/dc/elements/1.1/\">{language}</dc:language>"
        "</metadata><manifest>" + "".join(manifest) + "</manifest>"
        "<spine>" + "".join(spine) + "</spine></package>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buf.getvalue()


EPUB_TWO = [
    ("c2.xhtml", "Chapter Two", "BBB second chapter body."),
    ("c1.xhtml", "Chapter One", "AAA first chapter body."),
]


class TestHTMLParity:
    @pytest.mark.parametrize(
        "doc",
        [
            HTML_DOC,
            "<html><body><p>Plain.</p></body></html>",
            "<p>Fragment without envelope.</p>",
            "",
            "<html><body><p>A&#38b C&#65;D</p></body></html>",
        ],
    )
    async def test_parse_matches_across_backends(self, doc, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, "e.html", doc.encode("utf-8"))
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await HTMLModule().parse(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await HTMLModule().parse(path)
        if "&#38b" in doc or "&#65D" in doc:
            # Documented residual (see module docstring): the crate decodes
            # cleanly, html.parser leaks the closing markup as text.
            assert actual[0] == "A&b CAD"
            assert expected[0] == "A&b C&#65;D</p></body></html>"
        else:
            assert actual == expected

    @pytest.mark.parametrize(
        "name, content, score",
        [
            ("e.html", b"<html>", 0.9),
            ("e.htm", b"<html>", 0.9),
            ("e.txt", b"<html>", 0.7),
            ("noext", b"<!DOCTYPE html><p>", 0.7),
            ("noext", b"<HTML LANG=x>", 0.7),
            ("noext", b"plain prose", 0.0),
            ("noext", b"\xff\xfe", 0.0),
        ],
    )
    async def test_detect_matches_across_backends(self, name, content, score, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, name, content)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await HTMLModule().detect(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await HTMLModule().detect(path)
        assert actual == expected
        assert actual[0] == score


class TestEPUBParity:
    async def test_parse_matches_across_backends(self, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, "b.epub", _epub_bytes(EPUB_TWO))
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await EPUBModule().parse(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await EPUBModule().parse(path)
        assert actual == expected
        assert actual[1] == "T"
        assert [s["heading"] for s in actual[2]["sections"]] == ["Chapter Two", "Chapter One"]

    async def test_entities_substitute_in_chapters(self, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        chapters = [("c1.xhtml", "H", "A&#38b C&#65;D")]
        path = _write(tmp_path, "b.epub", _epub_bytes(chapters))
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await EPUBModule().parse(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await EPUBModule().parse(path)
        # No residual here: the chapter bytes normalize upstream, so both
        # sides decode cleanly.
        assert actual == expected
        assert actual[0] == "H\nA&b CAD"

    @pytest.mark.parametrize(
        "name, content, score",
        [
            ("b.epub", b"PK\x03\x04", 0.9),
            ("b.zip", b"PK\x03\x04", 0.2),
            ("noext", b"PK\x03\x04xxxx", 0.2),
            ("noext", b"plain prose", 0.0),
        ],
    )
    async def test_detect_matches_across_backends(self, name, content, score, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, name, content)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await EPUBModule().detect(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await EPUBModule().detect(path)
        assert actual == expected
        assert actual[0] == score

    @pytest.mark.parametrize("content", [b"not a zip", b"PK\x03\x04truncated"])
    async def test_corrupt_archives_raise_either_way(self, content, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, "b.epub", content)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(Exception):  # noqa: B017 - engines differ; failure itself is the contract
            await EPUBModule().parse(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ValueError):
            await EPUBModule().parse(path)


class TestTEIParity:
    @pytest.mark.parametrize("doc", [TEI_DOC, TEI_DOC.replace(' xmlns="http://www.tei-c.org/ns/1.0"', "")])
    async def test_parse_matches_across_backends(self, doc, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, "w.xml", doc.encode("utf-8"))
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await TEIXMLModule().parse(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await TEIXMLModule().parse(path)
        assert actual == expected

    @pytest.mark.parametrize(
        "name, content, score",
        [
            ("w.xml", TEI_DOC.encode(), 0.95),
            ("w.tei", b"<TEI><text/>", 0.7),
            ("w.txt", TEI_DOC.encode(), 0.0),
            ("w.xml", b"<root/>", 0.0),
        ],
    )
    async def test_detect_matches_across_backends(self, name, content, score, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, name, content)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await TEIXMLModule().detect(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await TEIXMLModule().detect(path)
        assert actual == expected
        assert actual[0] == score

    @pytest.mark.parametrize("content", [b"<TEI><text><p>x</q>", b"", b"not xml at all"])
    async def test_malformed_xml_raises_either_way(self, content, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, "w.xml", content)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(Exception):  # noqa: B017 - engines differ; failure itself is the contract
            await TEIXMLModule().parse(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ValueError):
            await TEIXMLModule().parse(path)


class TestDependencyGates:
    @pytest.mark.parametrize("module", [HTMLModule, EPUBModule, TEIXMLModule])
    async def test_rust_parses_without_optional_dependencies(self, module, tmp_path, monkeypatch):
        """The [documents] gate stays on the Python path only."""
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        import sys

        for name in ("bs4", "ebooklib", "lxml", "lxml.etree"):
            monkeypatch.delitem(sys.modules, name, raising=False)
            monkeypatch.setitem(sys.modules, name, None)
        if module is HTMLModule:
            path = _write(tmp_path, "e.html", b"<html><body><p>Hi.</p></body></html>")
        elif module is EPUBModule:
            path = _write(tmp_path, "b.epub", _epub_bytes(EPUB_TWO[:1]))
        else:
            path = _write(tmp_path, "w.xml", TEI_DOC.encode())
        text, title, metadata = await module().parse(path)
        assert text
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(RuntimeError, match="documents"):
            await module().parse(path)

    async def test_python_path_needs_no_wheel(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        path = _write(tmp_path, "e.html", b"<html><body><p>Hi.</p></body></html>")
        assert (await HTMLModule().parse(path))[0] == "Hi."
