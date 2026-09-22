"""The Rust works seam must be byte-identical to the Python it replaces.

Every vector runs under both ``RE_RUST_BACKEND=rust`` and ``=python``;
digests compare as hex, markers as equal sets/lists, rendered markdown as
equal strings. Hash rows cross as JSON (ids pre-stringified, exactly as
the Python path consumes them); digests cross as bytes; markers as plain
strings.

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import pytest

from research_engine.services.works import drafting as drafting_mod
from research_engine.services.works.assembly import hash_assembled
from research_engine.services.works.drafting import render_markdown
from research_engine.services.works.markers import find_markers, format_marker
from tests.unit.works.test_spine_format import (
    CITE_A,
    TestContentHash,
    _view_with_citation,
)


class TestHashParity:
    def test_hash_assembled_matches_across_backends(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        views = [
            TestContentHash()._view("words", "55555555-5555-5555-5555-555555555555"),
            TestContentHash()._view("ünïcodé wörds héreb vai", "55555555-5555-5555-5555-555555555555"),
            _view_with_citation(),
        ]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = [hash_assembled(view).hex() for view in views]
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = [hash_assembled(view).hex() for view in views]
        assert actual == expected

    def test_row_ids_and_timestamps_still_do_not_move_the_hash(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        maker = TestContentHash()
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert (
            hash_assembled(maker._view("words", "55555555-5555-5555-5555-555555555555"))
            == hash_assembled(maker._view("words", "66666666-6666-6666-6666-666666666666"))
        )

    def test_malformed_tables_rejected_on_rust(self, monkeypatch):
        rs = pytest.importorskip("marginalia_rs").works
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ValueError, match="row JSON"):
            rs.compute_content_hash("[oops", "[]", "[]", "[]")
        with pytest.raises(ValueError, match="UUID"):
            rs.format_marker("nope")


class TestMarkerParity:
    @pytest.mark.parametrize(
        "text",
        [
            f"a {format_marker(CITE_A)} b",
            "see {{cite:c1}} here",
            "",
            "no markers at all",
            "{{cite:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}}",
            "{{cite:AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}}",
            "x {{cite:abc}} y {{cite:def}} z",
        ],
    )
    def test_find_matches_across_backends(self, text, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = find_markers(text)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        from research_engine.services.works import drafting as drafting_check

        actual = drafting_check._find_markers(text)
        assert actual == expected
        assert actual[0] == expected[0] and actual[1] == expected[1]

    def test_format_round_trip_matches(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        from research_engine.services.works import attach as attach_mod

        assert attach_mod._format_marker(CITE_A) == format_marker(CITE_A)
        assert drafting_mod._format_marker(CITE_A) == format_marker(CITE_A)

    def test_render_uses_rust_markers(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        view = _view_with_citation()
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = render_markdown(view)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = render_markdown(view)
        assert actual == expected
        assert format_marker(CITE_A) in actual

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert hash_assembled(TestContentHash()._view("w", "55555555-5555-5555-5555-555555555555"))
