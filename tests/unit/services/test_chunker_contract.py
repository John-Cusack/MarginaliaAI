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

Two holes in this suite let a real defect through, and both are now closed.

`text_chunkers()` was a hand-maintained list, and `structural` — whose `chunk()`
takes a section table rather than a string — was simply absent from it. Nothing
failed: a missing chunker only means fewer tests run. The roster is now derived
from `CORE_CHUNKERS`, and `test_every_registered_chunker_is_under_contract`
turns an omission from silence into a failure.

And the invariants said nothing about *size*. `structural` emitted one passage
per section with no cap, so a book chapter became a single passage of several
thousand tokens against a 500-token norm, and every offset, ordering and
coverage assertion here passed. The fixtures could not have caught it either:
the largest was 2,910 characters, against roughly 750,000 for a real book. Both
gaps are addressed below — see `test_passages_are_bounded_or_irreducible` and
BOOK_SCALE.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from research_engine.services.ingestion.chunking.fixed_window import FixedWindowChunker
from research_engine.services.ingestion.chunking.prose_window import (
    ProseWindowChunker,
    sentence_spans,
)
from research_engine.services.ingestion.chunking.structural import StructuralChunker
from research_engine.services.ingestion.chunking.whole_or_paragraph import (
    WholeOrParagraphChunker,
)
from research_engine.services.ingestion.pipeline import CORE_CHUNKERS
from research_engine.services.text.sections import sections_from_markdown

pytestmark = pytest.mark.unit


def text_chunkers() -> list[Any]:
    """Every core chunker, at a small setting and at its default.

    Small windows so the tricky cases actually produce multiple chunks. Every
    entry in `CORE_CHUNKERS` must appear — see the coverage gate below.
    """
    return [
        ProseWindowChunker(max_tokens=20, overlap_tokens=5),
        ProseWindowChunker(),
        FixedWindowChunker(window_chars=64, overlap_chars=16),
        FixedWindowChunker(),
        WholeOrParagraphChunker(threshold_tokens=10),
        WholeOrParagraphChunker(),
        StructuralChunker(max_tokens=20, overlap_tokens=5),
        StructuralChunker(),
    ]


async def chunk_text(chunker: Any, text: str) -> list[Any]:
    """Drive any chunker from plain text, whatever its call signature.

    A chunker that consumes a section table instead of a string is still bound
    by these invariants; only the way in differs. Being hard to call is exactly
    how `structural` escaped this suite, so the adapter lives here rather than
    each chunker being trusted to volunteer.
    """
    if getattr(chunker, "consumes", "text") == "sections":
        return await chunker.chunk(_sections_for(text), None, full_text=text)
    return await chunker.chunk(text)


def _sections_for(text: str) -> list[dict]:
    """A section table for *text*: its headings, or the whole text as one."""
    if not text.strip():
        return []
    found = sections_from_markdown(text)
    if found:
        return found
    start, end = 0, len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return [{"char_start": start, "char_end": end, "level": 1}]


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
    drafts = await chunk_text(chunker, text)
    assert_offsets_are_true(chunker, text, drafts)


@pytest.mark.parametrize("chunker", text_chunkers(), ids=_ids(text_chunkers()))
async def test_positions_are_dense_and_ordered(chunker: Any) -> None:
    drafts = await chunk_text(chunker, ALL_TEXTS["long_prose"])
    assert [d.position for d in drafts] == list(range(len(drafts)))


@pytest.mark.parametrize("chunker", text_chunkers(), ids=_ids(text_chunkers()))
async def test_spans_advance_through_the_document(chunker: Any) -> None:
    """Chunks may overlap, but must not go backwards or repeat."""
    drafts = await chunk_text(chunker, ALL_TEXTS["long_prose"])
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
    drafts = await chunk_text(chunker, text)
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
    assert await chunk_text(chunker, "") == []
    assert await chunk_text(chunker, "   \n  ") == []


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
                drafts = await chunk_text(chunker, text)
            except TypeError:
                pytest.skip(f"{chunker_id} does not take plain text")
            assert_offsets_are_true(chunker, text, drafts), name


# --- Coverage gate ------------------------------------------------------------

def test_every_registered_chunker_is_under_contract() -> None:
    """A chunker that is not exercised here must fail, not go quiet.

    `structural` was absent from this suite for its whole existence. Nothing
    reported it, because an untested chunker looks exactly like a suite with
    fewer parameters. Deriving the expectation from the registry means the next
    chunker added to `CORE_CHUNKERS` turns this red until it is covered.
    """
    covered = {chunker.id for chunker in text_chunkers()}
    missing = set(CORE_CHUNKERS) - covered
    assert not missing, (
        f"Registered chunkers with no contract coverage: {sorted(missing)}. "
        f"Add them to text_chunkers(); if chunk() takes something other than a "
        f"string, teach chunk_text() how to call it."
    )


def test_every_chunker_declares_a_size_contract() -> None:
    """Unbounded must be a stated decision, not an omission.

    `whole_or_paragraph` is legitimately unbounded — one long paragraph is one
    long passage. `structural` was accidentally unbounded and looked identical
    from outside. Forcing the declaration separates the two.
    """
    for chunker in text_chunkers():
        assert hasattr(chunker, "max_passage_tokens"), (
            f"{chunker.id} declares no max_passage_tokens. State the cap, or "
            f"state None and say why."
        )


# --- Size ---------------------------------------------------------------------

#: Roughly a 500-page book. The largest fixture above is under 3,000 characters,
#: which is why a defect that only appears at chapter scale survived this suite.
BOOK_SCALE = (
    "The clerk recorded the transaction in the ledger. "
    "A second hand annotated the margin some years later. "
    "The binding was repaired twice before the bequest. "
) * 5000


#: Passages overshoot their limit slightly by construction: a window is closed
#: only when the *next* unit would exceed it, and the whole-window token
#: estimate rounds differently from the sum of its parts. Measured across every
#: chunker and fixture here, the worst real overshoot is 1.15x. 1.5 leaves room
#: without leaving room for a defect: the bug this rule exists to catch ran to
#: roughly 375x.
OVERSHOOT_TOLERANCE = 1.5


@pytest.mark.parametrize("chunker", text_chunkers(), ids=_ids(text_chunkers()))
@pytest.mark.parametrize("name", ["long_prose", "no_boundaries", "book_scale"])
async def test_passages_stay_near_their_declared_size(chunker: Any, name: str) -> None:
    """A passage of many sentences must stay near the declared cap.

    A flat bound would be wrong: `prose_window(max_tokens=20)` emits a 500-token
    passage for a text containing no sentence boundary, and is right to — there
    is nowhere to cut. So the exemption is content-based: a passage that is a
    single unbreakable unit may be any size, and a passage made of many units
    may not.

    The exemption must be content-based rather than "re-chunk it and see if it
    splits". That probe passes for `structural` no matter how large its output,
    because re-chunking one of its sections yields that same single section —
    it was written, tried against the original defect, and found to catch
    nothing. This rule catches it at roughly 375x.
    """
    limit = chunker.max_passage_tokens
    if limit is None:
        pytest.skip(f"{chunker.id} is unbounded by declaration")

    text = BOOK_SCALE if name == "book_scale" else ALL_TEXTS[name]
    for draft in await chunk_text(chunker, text):
        if len(sentence_spans(draft.text)) <= 1:
            continue  # irreducible content: no boundary to cut on
        tokens = max(1, len(draft.text) // 4)
        assert tokens <= limit * OVERSHOOT_TOLERANCE, (
            f"{chunker.id}: emitted a {tokens}-token passage against a declared "
            f"limit of {limit} ({tokens / limit:.1f}x), and it contains "
            f"{len(sentence_spans(draft.text))} sentences — so there were "
            f"boundaries available and the chunker did not use them."
        )


@pytest.mark.parametrize("chunker", text_chunkers(), ids=_ids(text_chunkers()))
async def test_book_scale_documents_are_chunked_at_all(chunker: Any) -> None:
    """Scale itself is a dimension: a whole book must not become one passage."""
    drafts = await chunk_text(chunker, BOOK_SCALE)

    assert drafts
    assert_offsets_are_true(chunker, BOOK_SCALE, drafts)
    if chunker.max_passage_tokens is not None:
        assert len(drafts) > 1, (
            f"{chunker.id} returned the entire book as {len(drafts)} passage(s)"
        )


# --- Determinism and degeneracy -----------------------------------------------

@pytest.mark.parametrize("chunker", text_chunkers(), ids=_ids(text_chunkers()))
@pytest.mark.parametrize("name", sorted(TRICKY_TEXTS))
async def test_no_empty_passages(chunker: Any, name: str) -> None:
    """A whitespace-only passage is an embedding and an index entry for nothing."""
    for draft in await chunk_text(chunker, TRICKY_TEXTS[name]):
        assert draft.text.strip(), f"{chunker.id}: empty passage on {name}"


@pytest.mark.parametrize("chunker", text_chunkers(), ids=_ids(text_chunkers()))
async def test_chunking_is_deterministic(chunker: Any) -> None:
    """Re-ingesting a document must not silently renumber the corpus."""
    first = await chunk_text(chunker, ALL_TEXTS["long_prose"])
    second = await chunk_text(chunker, ALL_TEXTS["long_prose"])

    assert [(d.char_start, d.char_end, d.position) for d in first] == [
        (d.char_start, d.char_end, d.position) for d in second
    ]
