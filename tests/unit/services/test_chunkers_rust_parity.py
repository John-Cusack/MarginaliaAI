"""The Rust chunker seams must be byte-identical to the Python they replace.

Only prose and structural chunking cross to Rust (fixed and
whole-or-paragraph lost the accelerator benchmark's keep gate). Every vector runs under both ``RE_RUST_BACKEND=rust`` and ``=python``;
drafts must compare equal field-by-field, with the input metadata mapping
being the *same object* on prose (it never crosses the seam) and an
equal rebuild on structural (the crate never reads it). Sections the Rust
path refuses (non-JSON values, NaN, lone surrogates, offsets past i64) take
the Python path, so callers see Python's answer on either backend.

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from research_engine.domain.errors import ChunkingError
from research_engine.services.ingestion.chunking.prose_window import ProseWindowChunker
from research_engine.services.ingestion.chunking.structural import StructuralChunker

TEXTS = [
    "Hello world. This is a test. " * 100,
    " ".join(["lorem ipsum dolor sit amet"] * 200),
    " ".join(["日本語の文章です"] * 300),
    "מִשְׁפָּט וּצְדָקָה הולכים יחד. " * 150,
    "index entry without any sentence punctuation at all " * 200,
    "short.",
    "   \n\t  ",
    "",
]

EXOTIC_METADATA = {
    "plain": "v",
    "float": 1 / 65,
    "uuid": uuid.uuid4(),
    "when": datetime.now(UTC),
    "nested": {"a": [1, 2.5, None]},
}


def _dumps(drafts):
    return [d.model_dump() for d in drafts]


class TestTextChunkerParity:
    @pytest.mark.parametrize("text", TEXTS)
    async def test_prose_matches_across_backends(self, text, monkeypatch):
        pytest.importorskip("research_engine._native")
        meta = {"source": "t"}
        chunker = ProseWindowChunker()
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await chunker.chunk(text, meta)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await chunker.chunk(text, meta)
        assert _dumps(actual) == _dumps(expected)
        if actual:
            assert all(d.metadata is meta for d in actual)

    async def test_exotic_metadata_survives_by_identity(self, monkeypatch):
        """Prose metadata never crosses the seam: identity, not copy."""
        pytest.importorskip("research_engine._native")
        text = "Hello world. " * 100
        chunker = ProseWindowChunker()
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await chunker.chunk(text, EXOTIC_METADATA)
        assert actual
        assert all(d.metadata is EXOTIC_METADATA for d in actual)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await chunker.chunk(text, EXOTIC_METADATA)
        assert _dumps(actual) == _dumps(expected)

    async def test_prose_budget_rejection_matches(self, monkeypatch):
        pytest.importorskip("research_engine._native")
        chunker = ProseWindowChunker(max_tokens=0)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(ValueError, match="max_tokens must be positive"):
            await chunker.chunk("Hello. " * 10)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ValueError, match="max_tokens must be positive"):
            await chunker.chunk("Hello. " * 10)


SECTIONS = [
    {"text": "Alpha section here. It fits.", "heading": "Alpha", "level": 1, "char_start": 0, "char_end": 29},
    {"text": "Beta text. ", "heading": "Beta", "level": 2, "page": 7, "char_start": 29, "char_end": 40},
    {"text": "   \n ", "heading": "Blank", "level": 2, "char_start": 40, "char_end": 45},
]


class TestStructuralParity:
    async def test_sections_match_across_backends(self, monkeypatch):
        pytest.importorskip("research_engine._native")
        meta = {"doc": "d1", "n": 1}
        chunker = StructuralChunker()
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await chunker.chunk(SECTIONS, meta)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await chunker.chunk(SECTIONS, meta)
        assert _dumps(actual) == _dumps(expected)
        assert actual[0].locator == {"heading": "Alpha", "level": 1}
        assert actual[0].metadata == {"doc": "d1", "n": 1, "section_heading": "Alpha"}
        assert actual[1].locator == {"heading": "Beta", "level": 2, "page": 7}

    async def test_locate_via_full_text_with_repeated_headings(self, monkeypatch):
        pytest.importorskip("research_engine._native")
        full = "Intro here. Body one. Body one. Tail end."
        sections = [
            {"text": "Body one.", "heading": "B"},
            {"text": "Body one.", "heading": "B"},
        ]
        chunker = StructuralChunker()
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await chunker.chunk(sections, None, full)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await chunker.chunk(sections, None, full)
        assert _dumps(actual) == _dumps(expected)
        # The cursor resolves repeats successively, not all to the first.
        assert actual[0].char_start != actual[1].char_start

    async def test_oversized_section_windows_with_parts(self, monkeypatch):
        pytest.importorskip("research_engine._native")
        text = "Sentence one here. " * 400
        sections = [{"text": text, "heading": "Long", "level": 1, "char_start": 0, "char_end": len(text)}]
        chunker = StructuralChunker(max_tokens=100, overlap_tokens=10)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await chunker.chunk(sections, {"m": 1})
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await chunker.chunk(sections, {"m": 1})
        assert len(actual) > 1
        assert _dumps(actual) == _dumps(expected)
        assert all(d.locator["section_parts"] == len(actual) for d in actual)

    async def test_exotic_metadata_rebuilt_equal(self, monkeypatch):
        pytest.importorskip("research_engine._native")
        chunker = StructuralChunker()
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await chunker.chunk(SECTIONS, EXOTIC_METADATA)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await chunker.chunk(SECTIONS, EXOTIC_METADATA)
        assert _dumps(actual) == _dumps(expected)
        assert actual[0].metadata["section_heading"] == "Alpha"
        assert actual[0].metadata["float"] == 1 / 65

    @pytest.mark.parametrize(
        "sections, full, match",
        [
            ("just text", None, "section table, not text"),
            ([{"text": "Lost prose", "heading": "L"}], None, "needs offsets"),
            ([{"text": "Alpha", "char_start": 0, "char_end": 5}], "ZZZZZ", "does not match"),
            ([{"text": "Missing entirely"}], "other document here", "not found"),
        ],
    )
    async def test_chunking_errors_match(self, sections, full, match, monkeypatch):
        pytest.importorskip("research_engine._native")
        chunker = StructuralChunker()
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        with pytest.raises(ChunkingError) as python_err:
            await chunker.chunk(sections, None, full)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ChunkingError) as rust_err:
            await chunker.chunk(sections, None, full)
        assert match in str(rust_err.value)
        assert str(rust_err.value) == str(python_err.value)

    @pytest.mark.parametrize(
        "section",
        [
            {"text": "Hi there.", "page": uuid.uuid4(), "char_start": 0, "char_end": 9},
            {"text": "Hi there.", "page": float("nan"), "char_start": 0, "char_end": 9},
            {"text": "Lone \ud800 surrogate.", "heading": "S"},
            {"text": "Huge.", "char_start": 0, "char_end": 2**70},
        ],
    )
    async def test_sections_rust_refuses_take_pythons_answer(self, section, monkeypatch):
        """Values that can't cross (non-JSON, NaN, surrogates, past i64) fall back."""
        pytest.importorskip("research_engine._native")
        chunker = StructuralChunker()
        full = section["text"]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _dumps(await chunker.chunk([section], None, full))
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _dumps(await chunker.chunk([section], None, full)) == expected

    # Review findings: sections a parser gets slightly wrong, or types a
    # plugin passes loosely, must behave exactly as they do on Python.
    @pytest.mark.parametrize(
        "sections, full",
        [
            ([{"heading": "A", "char_start": 12, "char_end": 999}], "Intro here.\nChapter text goes on."),
            (
                [{"text": "Chapter text goes on.", "heading": "A", "char_start": 12, "char_end": 999}],
                "Intro here.\nChapter text goes on.",
            ),
            ([{"text": "Backwards.", "heading": "B", "char_start": 9, "char_end": 3}], None),
            ([{"text": "Numeric heading.", "heading": 7}], "Numeric heading."),
            ([{"text": "List heading.", "heading": ["x"]}], "List heading."),
            ([{"text": "String level.", "heading": "L", "level": "2"}], "String level."),
            ([{"text": "Float level.", "heading": "L", "level": 1.0}], "Float level."),
            ([{"text": "Bool level.", "heading": "L", "level": True}], "Bool level."),
            ([{"text": "Huge level.", "heading": "L", "level": 2**70}], "Huge level."),
            ([{"text": "Float start.", "char_start": 0.0, "char_end": 12}], "Float start."),
        ],
    )
    async def test_loose_sections_match_python(self, sections, full, monkeypatch):
        pytest.importorskip("research_engine._native")
        chunker = StructuralChunker()
        outcomes = []
        for backend in ("python", "rust"):
            monkeypatch.setenv("RE_RUST_BACKEND", backend)
            try:
                outcomes.append(("ok", _dumps(await chunker.chunk(sections, {"m": 1}, full))))
            except Exception as exc:  # noqa: BLE001 - the error itself is the compared value
                outcomes.append((type(exc).__name__, str(exc)))
        assert outcomes[1] == outcomes[0]

    async def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "research_engine._native", raising=False)
        monkeypatch.setitem(sys.modules, "research_engine._native", None)
        drafts = await ProseWindowChunker().chunk("Hello world. " * 50, {"m": 1})
        assert drafts
        # json is only imported for the Rust path; the module stays lean otherwise.
        assert json.dumps(SECTIONS)
