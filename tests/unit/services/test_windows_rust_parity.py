"""The Rust windows seam must be byte-identical to the Python it replaces.

Every vector runs under both ``RE_RUST_BACKEND=rust`` and ``=python`` and
the two plans/windows must compare equal — spans, sources, texts, tokens —
with the bound node being the *same object*. Nodes cross as ``DocumentNode``
JSON (metadata floats ride along unread and unreturned; a 17-digit float
battery pins that), spans as integer pairs clamped at zero on malformed
input (a documented deviation: storage validates non-negative).

Rust-forced cases skip when the accelerator wheel is absent; the Python path
always runs.
"""

from __future__ import annotations

import uuid

import pytest

from research_engine.services.search.windows import (
    PassageWindowReader,
    _build_window,
    _passage_span,
    choose_window,
)
from research_engine.services.text.anchoring import Span
from tests.unit.services.test_search_windows import (
    FakeNodes,
    FakeTexts,
    node,
    passage,
)

DOC = uuid.uuid4()

LOUW = (
    Span(1_190, 1_290),
    [
        node(0, 200_000, 0, "Louw-Nida"),
        node(1_000, 1_400, 1, "Domain 56: Justice"),
        node(1_200, 1_268, 2, "56.29 κρίσις"),
    ],
    6_000,
    800,
)
JEW = (
    Span(110_000, 112_000),
    [
        node(0, 5_000_000, 0, "A Marginal Jew"),
        node(0, 900_000, 1, "Part Two"),
        node(100_000, 124_267, 2, "Chapter 14"),
    ],
    6_000,
    800,
)
HEBREW_CHAIN = [
    node(0, 50_000, 0, "WLC"),
    node(1_000, 2_000, 1, "מִשְׁפָּט"),
    node(1_200, 1_400, 2, "צְדָקָה"),
]

CHOOSE_VECTORS = [
    pytest.param(*LOUW, id="louw-climb"),
    pytest.param(*JEW, id="jew-cap"),
    pytest.param(Span(100_010, 100_100), JEW[1], 6_000, 800, id="chapter-start-slide"),
    pytest.param(Span(500_000, 501_000), [node(0, 23_198_553, 0, "TDNT")], 6_000, 800, id="tdnt-root"),
    pytest.param(Span(5_000, 5_200), [], 6_000, 800, id="no-ancestors"),
    pytest.param(Span(10, 60), [], 6_000, 800, id="near-zero"),
    pytest.param(Span(1_000, 9_000), [node(0, 50_000, 0, "doc")], 500, 200, id="floor"),
    pytest.param(Span(9_400, 9_800), HEBREW_CHAIN, 6_000, 800, id="hebrew-titles"),
    pytest.param(Span(9_400, 9_800), HEBREW_CHAIN, -100, -50, id="negative-budgets"),
    pytest.param(None, [], 6_000, 800, id="spanless"),
]


class TestChooseParity:
    @pytest.mark.parametrize("passage, chain, budget, minimum", CHOOSE_VECTORS)
    def test_rust_matches_python_plan_and_bound_object(
        self, passage, chain, budget, minimum, monkeypatch
    ):
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = choose_window(passage, chain, budget_chars=budget, min_chars=minimum)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = choose_window(passage, chain, budget_chars=budget, min_chars=minimum)
        assert actual == expected
        if actual is not None:
            assert actual.node is expected.node

    def test_metadata_floats_do_not_move_the_decision(self, monkeypatch):
        """The ≥17-digit decimal finding cannot surface: metadata is unread."""
        pytest.importorskip("marginalia_rs")
        chain = [node(0, 200_000, 0, "Root")]
        chain[0].metadata["score"] = 1 / 65
        chain.append(node(1_200, 1_268, 2, "Entry"))
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = choose_window(Span(1_190, 1_290), chain, budget_chars=6_000, min_chars=800)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = choose_window(Span(1_190, 1_290), chain, budget_chars=6_000, min_chars=800)
        assert actual == expected

    def test_negative_span_clamps_on_rust(self, monkeypatch):
        """Malformed-input deviation pin: the seam clamps, Python computes raw."""
        pytest.importorskip("marginalia_rs")
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        plan = choose_window(Span(-50, 100), [], budget_chars=500, min_chars=10)
        assert plan is not None
        assert plan.span.start >= 0

    def test_python_path_needs_no_wheel(self, monkeypatch):
        import sys

        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        monkeypatch.delitem(sys.modules, "marginalia_rs", raising=False)
        monkeypatch.setitem(sys.modules, "marginalia_rs", None)
        plan = choose_window(LOUW[0], LOUW[1], budget_chars=6_000, min_chars=800)
        assert plan.source == "node_window"
        assert plan.node is LOUW[1][0]


class TestBuildParity:
    @pytest.mark.parametrize(
        "raw",
        [
            " ".join(["lorem ipsum dolor"] * 400),
            "מִשְׁפָּט " * 300 + "padding " * 200,
            "   \n\t  ",
            "",
        ],
    )
    def test_rust_matches_python_window(self, raw, monkeypatch):
        pytest.importorskip("marginalia_rs")
        chain = HEBREW_CHAIN
        span = Span(1_100, 1_500)
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        plan = choose_window(span, chain, budget_chars=6_000, min_chars=100)
        fake = passage(uuid.uuid4(), span.start, span.end, None, raw)
        expected = _build_window(fake, plan, chain, raw)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = _build_window(fake, plan, chain, raw)
        if expected is None:
            assert actual is None
        else:
            assert actual.model_dump() == expected.model_dump()

    def test_spanless_passage_builds_without_a_floor(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        chain = HEBREW_CHAIN
        raw = "word " * 2000
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        plan = choose_window(Span(1_100, 1_500), chain, budget_chars=6_000, min_chars=100)
        spanless = passage(uuid.uuid4(), None, None, None, raw)
        expected = _build_window(spanless, plan, chain, raw)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = _build_window(spanless, plan, chain, raw)
        assert actual is not None and expected is not None
        assert actual.model_dump() == expected.model_dump()
        # The floor helper sees no offsets on either backend.
        assert _passage_span(spanless) is None


class TestReaderParity:
    def _reader(self, text):
        n0, n1 = node(0, 50_000, 0, "Root"), node(1_000, 2_000, 1, "Leaf מִשְׁפָּט")
        chains = {n1.id: [n0, n1], n0.id: [n0]}
        return PassageWindowReader(
            FakeNodes(chains), FakeTexts(text), max_tokens=1_500, min_tokens=200
        ), (n0, n1)

    async def test_read_matches_across_backends(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        text = " ".join([f"word{i}" for i in range(20_000)])
        reader, (n0, n1) = self._reader(text)
        pids = [uuid.uuid4() for _ in range(3)]
        hits = [
            passage(pids[0], 1_100, 1_500, n1.id, text[1_100:1_500]),
            passage(pids[1], 100, 200, n0.id, text[100:200]),
            passage(pids[2], None, None, None, ""),
        ]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        expected = await reader.read(hits)
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        actual = await reader.read(hits)
        assert set(actual) == set(expected)
        for pid in expected:
            assert actual[pid].model_dump() == expected[pid].model_dump()

    async def test_missing_text_yields_no_window_either_way(self, monkeypatch):
        pytest.importorskip("marginalia_rs")
        reader, (_, n1) = self._reader("")
        reader._texts = FakeTexts("", missing=True)
        hits = [passage(uuid.uuid4(), 1_100, 1_500, n1.id, "x")]
        monkeypatch.setenv("RE_RUST_BACKEND", "python")
        assert await reader.read(hits) == {}
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        assert await reader.read(hits) == {}


class TestSeamContracts:
    def test_malformed_inputs_are_value_errors(self, monkeypatch):
        rs = pytest.importorskip("marginalia_rs").chunk
        monkeypatch.setenv("RE_RUST_BACKEND", "rust")
        with pytest.raises(ValueError, match="DocumentNode"):
            rs.choose_window((1, 2), ["[not json"], budget_chars=100, min_chars=10)
        with pytest.raises(ValueError, match="DocumentNode"):
            rs.build_window((0, 10), ((0, 100), "node", None), ["[bad"], "x" * 120)
        with pytest.raises(ValueError, match="WindowSource"):
            rs.build_window((0, 10), ((0, 100), "shelf", None), [], "x" * 120)
        with pytest.raises(ValueError, match="UUID"):
            rs.build_window((0, 10), ((0, 100), "node", "nope"), [], "x" * 120)
