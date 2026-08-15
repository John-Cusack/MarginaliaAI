"""The contract every chunker owes, packaged so a pack can hold itself to it.

Three chunkers have now shipped violating the same invariant, each for its own
reason, and none was caught by a test:

* ``structural`` was absent from the core suite because its ``chunk()`` took a
  different argument, so it emitted whole book chapters unnoticed;
* ``prose_window`` knew only sentence boundaries, so a book index — which has
  almost no full stops — became one 22,131-token passage;
* ``verse_boundary``, in the Logos pack, splits on paragraphs, and a lexicon
  entry is frequently one paragraph.

The pattern is not carelessness, it is that every chunker knows one kind of seam
and real documents contain text with none of it. So the invariants live here,
importable, rather than in the core test tree where a pack cannot reach them.
A pack calls ``assert_chunker_contract`` in its own suite and is held to exactly
what core is held to.

The load-bearing one remains ``draft.text == text[draft.char_start:char_end]``.
The newest is ``ABSOLUTE_MAX_TOKENS``, which admits no exemption: a passage
larger than the embedding model accepts is truncated at embedding time, so it
sits in the corpus looking indexed while most of it cannot be found.
"""

from __future__ import annotations

import re
from typing import Any

#: No chunker may exceed this, whatever its reasoning. Sized well under the 8192
#: an embedding model like bge-m3 accepts, because a passage that is truncated
#: at embedding time is worse than one that is merely large: the text is stored,
#: counted, and unreachable.
ABSOLUTE_MAX_TOKENS = 2_000

#: A window closes only when the *next* unit would exceed it, and the
#: whole-window token estimate rounds differently from the sum of its parts, so
#: some overshoot is structural. Measured across the core chunkers, the worst
#: real overshoot is 1.15x.
OVERSHOOT_TOLERANCE = 1.5

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def approx_tokens(text: str) -> int:
    """The ~4-chars-per-token estimate every core chunker uses."""
    return max(1, len(text) // 4)


def _atoms(text: str) -> int:
    return len(_SENTENCE.split(text))


#: Shapes that have actually broken a chunker, plus the ordinary cases. An
#: `index` entry is here because a real one in the corpus is what exposed
#: `prose_window`; `lexicon` because a real one exposed `verse_boundary`.
CONTRACT_TEXTS: dict[str, str] = {
    "empty": "",
    "whitespace_only": "   \n\n\t  ",
    "single_sentence": "One sentence with no terminator",
    "prose": (
        "The archive holds letters. Each letter has a date. "
        "Some dates are approximate. Others are exact. "
    ) * 40,
    # No sentence punctuation at all, newline-delimited records.
    "index": "Index\n\nPage numbers correspond to the print edition.\n\n"
    + "".join(
        f"{word}, {n}, {n + 40}\n"
        for n, word in enumerate(
            ["abolition", "archive", "binding", "clerk", "codex", "deed"] * 400
        )
    ),
    # One unbroken paragraph, dense with abbreviations that look like sentence
    # ends but are not — the shape of a lexicon entry.
    "lexicon": (
        "בְּ Sem., Ug. UM §10:1, Akk. in bašū (cf. AHw. 112); cf. Arm. b-, "
        "Syr. b-, Mnd. b-, e.g. v. 3, cp. Gn. 1:1, Ex. 3:14, etc. "
    ) * 200,
    "no_boundaries": "word " * 2000,
    "cjk": "第一句話。第二句話。第三句話。" * 200,
    "crlf": "First line.\r\nSecond line.\r\n\r\nThird.",
    "book_scale": (
        "The clerk recorded the transaction in the ledger. "
        "A second hand annotated the margin some years later. "
    ) * 3000,
}


async def call_chunker(chunker: Any, text: str) -> list[Any]:
    """Drive a chunker from plain text, whatever shape its ``chunk()`` takes.

    Dispatches on the declared ``consumes`` rather than probing with exceptions.
    Being awkward to call is precisely how ``structural`` escaped its contract,
    so the awkwardness is handled here and not left to each caller.
    """
    if getattr(chunker, "consumes", "text") == "sections":
        if not text.strip():
            return []
        start, end = 0, len(text)
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        sections = [{"char_start": start, "char_end": end, "level": 1}]
        return await chunker.chunk(sections, None, full_text=text)
    return await chunker.chunk(text)


async def assert_chunker_contract(
    chunker: Any,
    *,
    texts: dict[str, str] | None = None,
    absolute_max_tokens: int = ABSOLUTE_MAX_TOKENS,
) -> None:
    """Assert every invariant a chunker owes. Raises ``AssertionError`` on any.

    Call this from a pack's own test suite::

        async def test_my_chunker_honours_the_contract():
            await assert_chunker_contract(MyChunker())

    A chunker must declare ``max_passage_tokens`` — ``None`` states that it is
    unbounded by design, which is a legitimate choice and a visible one. That
    declaration is required precisely because an accidental omission and a
    deliberate decision were previously indistinguishable.
    """
    label = getattr(chunker, "id", type(chunker).__name__)
    assert hasattr(chunker, "max_passage_tokens"), (
        f"{label} declares no max_passage_tokens. State the cap, or state None "
        f"to record that it is unbounded on purpose."
    )
    limit = chunker.max_passage_tokens

    for name, text in (texts or CONTRACT_TEXTS).items():
        drafts = await call_chunker(chunker, text)

        if not text.strip():
            assert drafts == [], f"{label}: emitted passages for empty input ({name})"
            continue

        assert drafts, f"{label}: emitted nothing for {name}"

        starts = [d.char_start for d in drafts]
        assert starts == sorted(starts), f"{label}: spans go backwards on {name}"
        assert len({(d.char_start, d.char_end) for d in drafts}) == len(drafts), (
            f"{label}: duplicate spans on {name}"
        )
        assert [d.position for d in drafts] == list(range(len(drafts))), (
            f"{label}: positions are not dense and ordered on {name}"
        )

        cursor = 0
        for draft in drafts:
            assert draft.char_start is not None and draft.char_end is not None, (
                f"{label}: passage without a span on {name}"
            )
            assert 0 <= draft.char_start <= draft.char_end <= len(text), (
                f"{label}: span ({draft.char_start}, {draft.char_end}) out of "
                f"bounds for {name} of length {len(text)}"
            )
            assert draft.text == text[draft.char_start : draft.char_end], (
                f"{label}: text does not match its own span on {name}.\n"
                f"  draft.text = {draft.text[:80]!r}\n"
                f"  slice      = {text[draft.char_start : draft.char_end][:80]!r}"
            )
            assert draft.text.strip(), f"{label}: empty passage on {name}"

            if draft.char_start > cursor:
                gap = text[cursor : draft.char_start]
                assert not gap.strip(), f"{label}: dropped content on {name}: {gap[:80]!r}"
            cursor = max(cursor, draft.char_end)

            tokens = approx_tokens(draft.text)
            assert tokens <= absolute_max_tokens, (
                f"{label}: emitted a {tokens:,}-token passage on {name}. Nothing "
                f"justifies this: an embedding model truncates it, so the tail "
                f"is stored, counted, and unreachable by search."
            )
            if limit is not None and _atoms(draft.text) > 1:
                assert tokens <= limit * OVERSHOOT_TOLERANCE, (
                    f"{label}: {tokens}-token passage against a declared limit "
                    f"of {limit} on {name}, across {_atoms(draft.text)} "
                    f"sentences — boundaries were available and unused."
                )

        assert not text[cursor:].strip(), f"{label}: dropped the tail of {name}"

        again = await call_chunker(chunker, text)
        assert [(d.char_start, d.char_end) for d in again] == [
            (d.char_start, d.char_end) for d in drafts
        ], f"{label}: chunking is not deterministic on {name}"
