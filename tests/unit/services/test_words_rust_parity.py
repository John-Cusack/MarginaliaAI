"""The Rust words-shaping seam must match the lookup service.

`english_reference` crosses as a dict with the same keys in the same
order; the `mapping` value is the lowercase outcome on both sides. The
five note helpers pin the exact `find()` texts, including the 6-ref
truncation with ellipsis. Two typed boundaries are pinned, not papered
over: a `mapping_type` the schema forbids raises `ValueError` on Rust
where Python echoes it raw, and a quoted homograph renders through
`{homograph!r}` on Python but raw on Rust (homographs are single
letters, so this never fires on real rows).

Rust-forced cases skip when the accelerator wheel is absent; the Python
path always runs.
"""

from __future__ import annotations

import pytest

from research_engine.services.words import lookup as lookup_mod
from research_engine.services.words.lookup import (
    _map_empty_note,
    _over_limit_note,
    _partials_note,
    _qere_note,
    _zero_result_note,
    english_reference,
)


class TestEnglishReferenceParity:
    @pytest.mark.parametrize(
        "row",
        [
            # (ref, to_ref, to_part, from_part, mapping_type, map_loaded)
            ("Gen.1.1", "Gen.1.1", "a", "b", "full", False),
            ("Gen.1.1", None, None, None, None, False),
            ("Gen.1.1", None, None, None, None, True),
            ("Gen.1.2", "Gen.1.3", None, None, "full", True),
            ("Gen.1.2", "Gen.1.3", "a", "b", "partial", True),
        ],
    )
    def test_outcomes_match_exactly(self, row, monkeypatch):
        pytest.importorskip("marginalia_rs")
        ref, to_ref, to_part, from_part, mapping_type, loaded = row
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = english_reference(
            ref, to_ref, to_part, from_part, mapping_type, map_loaded=loaded
        )
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = english_reference(ref, to_ref, to_part, from_part, mapping_type, map_loaded=loaded)
        assert actual == expected
        assert list(actual.keys()) == ["ref", "mapping", "part", "hebrew_part"]

    def test_mapping_values_are_lowercase_outcomes(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert english_reference("G", "E", None, None, "full", map_loaded=True)["mapping"] == "full"
        assert english_reference("G", None, None, None, None, map_loaded=True)["mapping"] == "same"
        assert (
            english_reference("G", "E", None, None, "full", map_loaded=False)["mapping"]
            == "unmapped"
        )
        assert english_reference("G", "E", None, None, "full", map_loaded=False)["ref"] is None

    def test_forbidden_mapping_type_rejected_on_rust(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ValueError, match="mapping_type"):
            english_reference("G", "E", None, None, "weird", map_loaded=True)
        with pytest.raises(ValueError, match="mapping_type"):
            english_reference("G", "E", None, None, None, map_loaded=True)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        echoed = english_reference("G", "E", None, None, "weird", map_loaded=True)
        assert echoed["mapping"] == "weird"


class TestWordsNotesParity:
    @pytest.mark.parametrize(
        ("strong", "language", "homograph"),
        [
            ("4941", "he", None),
            ("4941", "he", ""),
            ("4941", "he", "a"),
            ("1", "ar", None),
        ],
    )
    def test_zero_result_notes_match(self, strong, language, homograph, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _zero_result_note(strong, language, homograph)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _zero_result_note(strong, language, homograph) == expected
        assert f"Strong's {strong}" in expected

    def test_quoted_homograph_renders_raw_on_rust(self, monkeypatch):
        """Python's `{homograph!r}` escapes quotes; the seam renders raw.
        Homographs are single letters, so the boundary never fires on rows —
        but it is pinned here so neither side can drift into the other."""
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _zero_result_note("4941", "he", "a'b")
        # `!r` picks double quotes when the value holds a single quote.
        assert 'with homograph "a\'b"' in expected
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = _zero_result_note("4941", "he", "a'b")
        assert "with homograph 'a'b'" in actual
        assert actual != expected

    def test_map_empty_note_matches(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _map_empty_note()
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _map_empty_note() == expected
        assert "1,978 mappings" in expected

    @pytest.mark.parametrize("total", [2001, 2500, 100000])
    def test_over_limit_notes_match(self, total, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _over_limit_note(total)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _over_limit_note(total) == expected
        assert "over the 2000 limit" in expected

    @pytest.mark.parametrize("count", [1, 3, 1978])
    def test_partials_notes_match(self, count, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _partials_note(count)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _partials_note(count) == expected

    @pytest.mark.parametrize(
        "refs",
        [
            [],
            ["Gen.1.1"],
            ["Gen.1.1", "Ex.2.2"],
            [f"Gen.1.{i}" for i in range(1, 7)],
            [f"Gen.1.{i}" for i in range(1, 9)],
        ],
    )
    def test_qere_notes_match(self, refs, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _qere_note(refs)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _qere_note(refs) == expected
        if len(refs) > 6:
            assert "…" in expected
            assert "Gen.1.7" not in expected.split(")")[0]

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert lookup_mod._partials_note(2).startswith("2 occurrence(s)")
        assert (
            lookup_mod.english_reference("G", None, None, None, None, map_loaded=True)["mapping"]
            == "same"
        )
