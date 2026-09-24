"""The Rust text seam must be byte-identical to the Python it replaces.

Every vector runs under both ``RE_RUST_BACKEND=rust`` and ``=python`` and the
two answers must compare equal — exact strings, exact index maps, exact tiers.
A divergence is a parity bug in the seam, never an acceptable difference, so
the suite fails rather than snapshotting. The Python path is the permanent
fallback (and rollback switch), so it is pinned here too: forcing ``python``
must equal calling ``normalize.*`` directly.

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import os
import uuid

import pytest

from research_engine import _rust as rust_backend
from research_engine.services.text import normalize as normalize_py
from research_engine.services.verification import QuoteVerifier, Tier
from research_engine.services.verification.quote import _find_folded

DOC = uuid.uuid4()

SOURCE = (
    "The prophets pair two words. He requires “justice and righteousness” of "
    "every ruler, a phrase the translations render un-\nevenly, and Amos 5:24 "
    "makes it a flood. “Let justice roll down like waters.”"
)

HEBREW = "מִשְׁפָּט and צְדָקָה walk together here"

#: Fold vectors chosen for the seams that have bitten before: linebreak
#: hyphens, soft hyphens, combining marks (per-character NFKC exists for
#: these), ASCII-incompatible whitespace, and non-ASCII text *before* the
#: match (byte-vs-character offsets corrupt anchors there).
FOLD_VECTORS = [
    "",
    "   ",
    "plain ascii",
    "  padded ascii  ",
    "fis-\ncal",
    "Anglo-\nSaxon",
    "“quoted” — dash",
    "soft\xadhyphen",
    "decomposed é vs é",
    HEBREW,
    "prefix ‹‹ " + HEBREW + " ›› suffix",
    " narrow  spaces ",
    "line sepand control",
    "“Let justice roll down like waters.”",
    SOURCE,
    # OCR'd book shape: thousands of line-break hyphens, with non-ASCII text
    # before each so a byte/char offset slip would move every later anchor.
    "".join(f"δικαιοσύνη {i} fis-\ncal Anglo-\nSaxon " for i in range(2000)),
]


def _fold_all(text: str) -> tuple:
    from research_engine.services.verification import quote as quote_mod

    return (
        quote_mod._normalize(text),
        quote_mod._normalize_for_matching(text),
        quote_mod._normalize_with_map(text),
    )


class TestFoldParity:
    @pytest.mark.parametrize("raw", FOLD_VECTORS)
    def test_rust_matches_python_output_and_map(self, raw, monkeypatch):
        marginalia_rs = pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _fold_all(raw)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = _fold_all(raw)
        assert actual[0] == expected[0]
        assert actual[1] == expected[1]
        assert actual[2][0] == expected[2][0]
        assert actual[2][1] == expected[2][1]
        # The map is consulted past non-ASCII text; spot-check the seam's
        # own answers against the native module, not just against itself.
        assert actual[0] == marginalia_rs.text.normalize(raw)
        assert actual[2] == marginalia_rs.text.normalize_with_map(raw)

    @pytest.mark.parametrize("raw", FOLD_VECTORS)
    def test_python_path_equals_direct_calls(self, raw, monkeypatch):
        """Rollback pin: forced-python is exactly the old behavior."""
        from research_engine.services.verification import quote as quote_mod

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert quote_mod._normalize(raw) == normalize_py.normalize(raw)
        assert (
            quote_mod._normalize_for_matching(raw)
            == normalize_py.normalize_for_matching(raw)
        )
        assert quote_mod._normalize_with_map(raw) == normalize_py.normalize_with_map(raw)

    def test_find_folded_agrees_past_non_ascii(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        raw = "préface «« " + HEBREW + " »» " + SOURCE
        needle = normalize_py.normalize_for_matching("“justice and righteousness”")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _find_folded(raw, needle)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = _find_folded(raw, needle)
        assert actual == expected
        assert actual is not None
        assert raw[actual.start : actual.end] == "“justice and righteousness”"


class TestSurrogateFallback:
    def test_lone_surrogate_takes_the_python_path(self, monkeypatch):
        """A lone surrogate can't cross into Rust; the wrappers answer from Python."""
        pytest.importorskip("marginalia_rs")
        raw = "lone \ud800 surrogate, fis-\ncal"
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = _fold_all(raw)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert _fold_all(raw) == expected


class TestTierParity:
    def _verifier(self):
        from tests.unit.services.test_quote_verification import (
            FakeDocuments,
            FakePassages,
            FakeTexts,
        )

        raw = {DOC: SOURCE}
        return QuoteVerifier(FakeTexts(raw), FakePassages(raw), FakeDocuments())

    @pytest.mark.parametrize(
        "quote",
        [
            "He requires “justice and righteousness” of every ruler",
            '"justice and righteousness"',
            "render un-\nevenly, and Amos",
            "a flood that never happened here",
            "makes it a FLOOD",
        ],
    )
    async def test_tiers_and_spans_match_across_backends(self, quote, monkeypatch):
        pytest.importorskip("marginalia_rs")
        verifier = self._verifier()
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await verifier.verify(quote, DOC)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await verifier.verify(quote, DOC)
        assert actual.tier == expected.tier
        assert actual.model_dump() == expected.model_dump()

    async def test_exact_stays_exact_under_rust(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        result = await self._verifier().verify(
            "makes it a flood.", DOC
        )
        assert result.tier == Tier.EXACT


class TestBackendSwitch:
    def test_auto_falls_back_without_the_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "auto")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        # An unimportable accelerator is auto, not an error.
        assert rust_backend.backend() == "python"
        assert rust_backend.rust_text() is None

    def test_rust_mode_missing_wheel_raises(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        with pytest.raises(RuntimeError, match="accelerated"):
            rust_backend.backend()

    def test_python_mode_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        assert rust_backend.backend() == "python"

    def test_unknown_mode_rejected(self, monkeypatch):
        monkeypatch.setenv("RE_RUST_BACKEND", "native")
        with pytest.raises(ValueError, match="RE_RUST_BACKEND"):
            rust_backend.backend()

    def test_default_is_auto(self, monkeypatch):
        monkeypatch.delenv("RE_RUST_BACKEND", raising=False)
        assert os.environ.get("RE_RUST_BACKEND") is None
        assert rust_backend.backend() in ("rust", "python")
