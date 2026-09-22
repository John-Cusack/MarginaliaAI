"""PDF detect parity plus the documented keep-Python parse decision.

Only `detect` cuts over (pure magic bytes). `parse` deliberately stays
Python: page text is engine output — fitz wraps long lines and trims page
edges where the Rust port's pdf-extract does neither — so routing it
through `marginalia_rs` would change extracted text on real PDFs. That is
a byte-identity violation, reported under HALT, not routed around: a
versioned re-ingest migration owns any future engine change.

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import pytest

from research_engine.modules.pdf_text import PDFTextModule


def _write(tmp_path, name, content: bytes):
    path = tmp_path / name
    path.write_bytes(content)
    return path


class TestPDFDetectParity:
    @pytest.mark.parametrize(
        "name, content, score",
        [
            ("d.pdf", b"%PDF-1.7", 0.9),
            ("d.PDF", b"%PDF-1.7", 0.9),
            ("d.txt", b"%PDF-1.7", 0.9),
            ("noext", b"%PDF-1.7 rest", 0.9),
            ("noext", b"%PD", 0.0),
            ("noext", b"", 0.0),
            ("noext", b"plain prose", 0.0),
        ],
    )
    async def test_detect_matches_across_backends(self, name, content, score, tmp_path, monkeypatch):
        pytest.importorskip("marginalia_rs")
        path = _write(tmp_path, name, content)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await PDFTextModule().detect(path)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await PDFTextModule().detect(path)
        assert actual == expected
        assert actual[0] == score

    async def test_python_path_needs_no_wheel(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        path = _write(tmp_path, "d.pdf", b"%PDF-1.7")
        assert await PDFTextModule().detect(path) == (
            0.9,
            "extension '.pdf' matches PDF",
        )


    async def test_no_rust_parse_path_exists(self, monkeypatch):
        """The keep-Python decision, pinned: no backend switch may route here."""
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        import inspect

        import research_engine.modules.pdf_text as pdf_mod

        assert not hasattr(pdf_mod, "_parse_pdf_rs")
        assert "rust_parse" not in inspect.getsource(pdf_mod.PDFTextModule.parse)
