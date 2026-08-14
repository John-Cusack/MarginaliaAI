"""The chunker offset invariant.

    For every chunker, every input text, and every emitted draft:
        draft.text == text[draft.char_start:draft.char_end]

This is the load-bearing test of P1. Every feature that addresses a span of a
document — pin-cites, quote verification, annotations, re-chunking — is built on
passage offsets being true. Before this test existed, `prose_window` emitted
`byte_start: 0` for every passage in the corpus and nothing noticed.

It mechanically forbids the two ways offsets go wrong:

  * altering the text after computing the span (`" ".join(...)`, `.strip()`)
  * computing the span against something other than the input text

Plugin-supplied chunkers are held to the same contract by the `contract`-marked
test at the bottom.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from research_engine.services.ingestion.chunking.fixed_window import FixedWindowChunker
from research_engine.services.ingestion.chunking.prose_window import ProseWindowChunker
from research_engine.services.ingestion.chunking.whole_or_paragraph import (
    WholeOrParagraphChunker,
)

pytestmark = pytest.mark.unit


def text_chunkers() -> list[Any]:
    """Every core chunker whose `chunk()` takes a plain string.

    Small windows so the tricky cases actually produce multiple chunks.
    """
    return [
        ProseWindowChunker(max_tokens=20, overlap_tokens=5),
        ProseWindowChunker(),
        FixedWindowChunker(window_chars=64, overlap_chars=16),
        FixedWindowChunker(),
        WholeOrParagraphChunker(threshold_tokens=10),
        WholeOrParagraphChunker(),
    ]


def _ids(chunkers: list[Any]) -> list[str]:
    return [f"{c.id}-{i}" for i, c in enumerate(chunkers)]


TRICKY_TEXTS: dict[str, str] = {
    "empty": "",
    "whitespace_only": "   \n\n\t  ",
    "single_sentence": "One sentence with no terminator",
    "leading_whitespace": "\n\n   Indented opening. And a second sentence.",
    "trailing_whitespace": "A sentence. Another one.   \n\n",
    "double_spaced": "First.  Second.  Third.  Fourth.",
    "paragraph_breaks": "Para one line one.\n\nPara two line one.\n\n\nPara three.",
    "crlf": "First line.\r\nSecond line.\r\n\r\nThird.",
    "tabs_between": "Alpha.\t\tBeta.\t\tGamma.",
    "no_boundaries": "word " * 400,
    "unicode_nfc": "Café société. Über größer. Naïve façade.",
    "combining_marks": "Café société. Über größer.",
    "cjk": "第一句話。第二句話。第三句話。" * 20,
    "emoji": "Look 👨‍👩‍👧‍👦 here. And 🇬🇧 there. Plus 🏳️‍🌈 more.",
    "soft_hyphen": "Hy­phen­ated wor­ds every­where. Second sentence.",
    "abbreviations": "Dr. Smith met Mr. Jones. They discussed e.g. the matter.",
    "quotes": '"Quoted opening." Then a reply. “Curly quotes.” Done.',
    "ellipsis": "Trailing thought... And the next. Another one.",
    "long_prose": (
        "The archive holds letters. Each letter has a date. "
        "Some dates are approximate. Others are exact. "
    ) * 30,
    "single_char": "x",
    "only_periods": ".....",
    "mixed_scripts": "English text. Русский текст. العربية نص. 日本語テキスト.",
}


def _random_texts(n: int = 25) -> dict[str, str]:
    """Deterministic pseudo-random inputs, to cover shapes nobody thought of."""
    rng = random.Random(20260810)
    alphabet = "abcdefg .!?\n\t é中"
    out = {}
    for i in range(n):
        length = rng.randint(0, 600)
        out[f"random_{i}"] = "".join(rng.choice(alphabet) for _ in range(length))
    return out


ALL_TEXTS = {**TRICKY_TEXTS, **_random_texts()}


def assert_offsets_are_true(chunker: Any, text: str, drafts: list[Any]) -> None:
    for draft in drafts:
        assert draft.char_start is not None, f"{chunker.id}: char_start is None"
        assert draft.char_end is not None, f"{chunker.id}: char_end is None"
        assert 0 <= draft.char_start <= draft.char_end <= len(text), (
            f"{chunker.id}: span ({draft.char_start}, {draft.char_end}) "
            f"out of bounds for text of length {len(text)}"
        )
        sliced = text[draft.char_start : draft.char_end]
        assert draft.text == sliced, (
            f"{chunker.id}: text does not match its own span.\n"
            f"  draft.text = {draft.text!r}\n"
            f"  slice      = {sliced!r}"
        )


@pytest.mark.parametrize("chunker", text_chunkers(), ids=_ids(text_chunkers()))
@pytest.mark.parametrize("name", sorted(ALL_TEXTS))
async def test_draft_text_equals_its_own_span(chunker: Any, name: str) -> None:
    text = ALL_TEXTS[name]
    drafts = await chunker.chunk(text)
    assert_offsets_are_true(chunker, text, drafts)


@pytest.mark.parametrize("chunker", text_chunkers(), ids=_ids(text_chunkers()))
async def test_positions_are_dense_and_ordered(chunker: Any) -> None:
    drafts = await chunker.chunk(ALL_TEXTS["long_prose"])
    assert [d.position for d in drafts] == list(range(len(drafts)))


@pytest.mark.parametrize("chunker", text_chunkers(), ids=_ids(text_chunkers()))
async def test_spans_advance_through_the_document(chunker: Any) -> None:
    """Chunks may overlap, but must not go backwards or repeat."""
    drafts = await chunker.chunk(ALL_TEXTS["long_prose"])
    starts = [d.char_start for d in drafts]
    assert starts == sorted(starts)
    assert len({(d.char_start, d.char_end) for d in drafts}) == len(drafts)


@pytest.mark.parametrize("chunker", text_chunkers(), ids=_ids(text_chunkers()))
async def test_whole_document_is_covered(chunker: Any) -> None:
    """No content may be dropped between consecutive chunks.

    A gap means text that exists in the document is in no passage, so it is
    unsearchable and uncitable. Whitespace-only gaps are fine.
    """
    text = ALL_TEXTS["long_prose"]
    drafts = await chunker.chunk(text)
    assert drafts

    cursor = 0
    for draft in drafts:
        if draft.char_start > cursor:
            gap = text[cursor : draft.char_start]
            assert not gap.strip(), f"{chunker.id}: dropped content {gap!r}"
        cursor = max(cursor, draft.char_end)
    assert not text[cursor:].strip(), f"{chunker.id}: dropped tail {text[cursor:]!r}"


@pytest.mark.parametrize("chunker", text_chunkers(), ids=_ids(text_chunkers()))
async def test_empty_input_yields_no_drafts(chunker: Any) -> None:
    assert await chunker.chunk("") == []
    assert await chunker.chunk("   \n  ") == []


async def test_prose_window_preserves_paragraph_structure() -> None:
    """The old implementation rebuilt chunks with `" ".join(sentences)`, which
    collapsed every whitespace run and made offsets unrecoverable. Slicing from
    the first sentence's start to the last sentence's end keeps it intact.
    """
    text = "First para line.\n\nSecond para line. Third sentence."
    drafts = await ProseWindowChunker().chunk(text)
    assert len(drafts) == 1
    assert "\n\n" in drafts[0].text


async def test_fixed_window_does_not_strip_its_text_away_from_its_span() -> None:
    """The old implementation `.strip()`ed after computing the span."""
    text = "   " + ("a" * 100) + "   "
    drafts = await FixedWindowChunker(window_chars=50, overlap_chars=0).chunk(text)
    assert_offsets_are_true(FixedWindowChunker(), text, drafts)


async def test_chunker_versions_were_bumped() -> None:
    """Output text changed, so stored passages under the old version are stale.

    P1-5's re-anchoring relies on old and new coexisting under different
    versions; that only works if the version actually differs.
    """
    assert ProseWindowChunker.version != "1.0"
    assert FixedWindowChunker.version != "1.0"
    assert WholeOrParagraphChunker.version != "1.0"


@pytest.mark.contract
async def test_plugin_chunkers_satisfy_the_same_invariant() -> None:
    """Any chunker a pack registers is held to the offset contract too.

    Skips when no packs are loaded, so the core suite stays hermetic.
    """
    from research_engine.plugins import registry as reg_mod

    registry = reg_mod._global_registry
    if registry is None:
        pytest.skip("No plugin registry loaded")

    plugin_chunkers = registry._chunkers
    if not plugin_chunkers:
        pytest.skip("No plugin chunkers registered")

    for chunker_id, factory in plugin_chunkers.items():
        chunker = factory() if isinstance(factory, type) else factory
        for name, text in TRICKY_TEXTS.items():
            try:
                drafts = await chunker.chunk(text)
            except TypeError:
                pytest.skip(f"{chunker_id} does not take plain text")
            assert_offsets_are_true(chunker, text, drafts), name
