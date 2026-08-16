"""Structural chunking: sections as the addressing unit, capped for retrieval.

A section is a unit of authorship, not of retrieval. Chapters run to thousands
of tokens, so the chunker keeps the section as the thing a passage belongs to
while capping the passage itself — and the span contract has to survive that
split, because everything downstream addresses the canonical text.
"""

from __future__ import annotations

import pytest

from research_engine.domain.errors import ChunkingError
from research_engine.services.ingestion.chunking.structural import StructuralChunker

# ~120 tokens per sentence at the 4-chars-per-token estimate the chunkers use.
LONG_SENTENCE = ("The clerk recorded every detail of the transaction. " * 10).strip()


def build_document(section_bodies: list[str]) -> tuple[str, list[dict]]:
    """Canonical text plus the section table addressing it."""
    text_parts: list[str] = []
    sections: list[dict] = []
    cursor = 0
    for index, body in enumerate(section_bodies):
        chunk = f"Heading {index}\n{body}"
        sections.append(
            {
                "char_start": cursor,
                "char_end": cursor + len(chunk),
                "heading": f"Heading {index}",
                "level": 1,
            }
        )
        text_parts.append(chunk)
        cursor += len(chunk) + 2
    return "\n\n".join(text_parts), sections


async def test_a_small_section_stays_one_passage():
    text, sections = build_document(["Short body."])

    drafts = await StructuralChunker().chunk(sections, None, full_text=text)

    assert len(drafts) == 1
    assert drafts[0].locator["heading"] == "Heading 0"
    assert "section_part" not in drafts[0].locator


async def test_an_oversized_section_is_split_into_windows():
    text, sections = build_document([LONG_SENTENCE])

    drafts = await StructuralChunker(max_tokens=60).chunk(sections, None, full_text=text)

    assert len(drafts) > 1
    assert all(draft.chunker == "structural" for draft in drafts)
    assert all(draft.token_count <= 120 for draft in drafts)


async def test_split_passages_keep_addressing_the_canonical_text():
    """The contract every passage owes, preserved through the shift."""
    text, sections = build_document([LONG_SENTENCE, LONG_SENTENCE])

    drafts = await StructuralChunker(max_tokens=60).chunk(sections, None, full_text=text)

    assert len(drafts) > 2
    for draft in drafts:
        assert text[draft.char_start : draft.char_end] == draft.text


async def test_every_fragment_carries_its_heading():
    """A fragment that lost its section is the disconnected chunk to avoid."""
    text, sections = build_document([LONG_SENTENCE])

    drafts = await StructuralChunker(max_tokens=60).chunk(sections, None, full_text=text)

    assert all(draft.locator["heading"] == "Heading 0" for draft in drafts)
    assert [draft.locator["section_part"] for draft in drafts] == list(
        range(1, len(drafts) + 1)
    )
    assert all(draft.locator["section_parts"] == len(drafts) for draft in drafts)


async def test_positions_stay_contiguous_across_sections():
    """Positions number the document, not each section."""
    text, sections = build_document([LONG_SENTENCE, "Short.", LONG_SENTENCE])

    drafts = await StructuralChunker(max_tokens=60).chunk(sections, None, full_text=text)

    assert [draft.position for draft in drafts] == list(range(len(drafts)))


async def test_passages_stay_in_document_order():
    text, sections = build_document([LONG_SENTENCE, "Short.", LONG_SENTENCE])

    drafts = await StructuralChunker(max_tokens=60).chunk(sections, None, full_text=text)

    starts = [draft.char_start for draft in drafts]
    assert starts == sorted(starts)


async def test_sections_may_omit_text_and_be_read_from_the_canonical_text():
    text, sections = build_document(["Short body."])
    for section in sections:
        section.pop("text", None)

    drafts = await StructuralChunker().chunk(sections, None, full_text=text)

    assert drafts[0].text == text[drafts[0].char_start : drafts[0].char_end]


async def test_a_section_with_neither_text_nor_full_text_is_refused():
    with pytest.raises(ChunkingError, match="offsets"):
        await StructuralChunker().chunk([{"text": "Body without a home."}], None)


async def test_a_dense_section_inside_a_latin_document_is_still_capped() -> None:
    """A document average is not a bound, and a section is where that shows.

    Measured against the document, a Greek section of a mostly-English book
    reads as English: this exact shape was estimated at 472 tokens and came to
    947 real ones, past a declared cap of 500, because the rate it was judged
    against belonged to the book rather than to the section.
    """
    english = "The clerk recorded the transaction in the ledger. " * 400
    greek = "λόγος πρὸς τὸν θεόν καὶ θεὸς ἦν ὁ λόγος οὗτος ἦν ἐν ἀρχῇ " * 22
    text = english + greek
    sections = [
        {"char_start": 0, "char_end": len(english), "level": 1},
        {"char_start": len(english), "char_end": len(text), "level": 1},
    ]

    chunker = StructuralChunker()
    drafts = await chunker.chunk(sections, None, full_text=text)

    # Judged the way the contract judges: non-ASCII at ~1.5 chars per token,
    # independent of whatever the chunker believed about this document.
    def independent_tokens(chunk: str) -> int:
        dense = sum(1 for char in chunk if ord(char) >= 128)
        return int((len(chunk) - dense) / 4.0 + dense / 1.5)

    worst = max(independent_tokens(d.text) for d in drafts)
    assert worst <= chunker.max_passage_tokens * 1.5, (
        f"emitted a {worst}-token passage against a {chunker.max_passage_tokens} "
        f"cap; the Greek section was measured with the book's rate, not its own"
    )
