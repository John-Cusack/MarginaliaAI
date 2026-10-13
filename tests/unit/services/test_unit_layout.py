"""Units laid out as canonical text: every span quotes exactly what it claims.

The guarantees later stages lean on: a unit's node spans its text alone (so a
quotation of a verse resolves to the verse), every window is the canonical
text verbatim, a supplied-word mark lands on the supplied words, and the pack's
chunker re-derives the same windows from the stored text — which is what lets
`reindex chunks` rebuild a document without the reader that made it.
"""

from __future__ import annotations

import pytest

from research_engine.services.ingestion.pipeline import get_chunker
from research_engine_sdk.testing import assert_chunker_contract
from research_engine_sdk.units import (
    LayoutError,
    Unit,
    UnitDocument,
    UnitMark,
    UnitWindowChunker,
    layout,
)

pytestmark = pytest.mark.unit

GEN_1 = UnitDocument(
    title="Genesis 1",
    source="test:kjv:Gen.1",
    units=(
        Unit(ref="Genesis 1:1", label="1",
             text="In the beginning God created the heaven and the earth."),
        Unit(ref="Genesis 1:2", label="2",
             text="And the earth was without form, and void; and darkness was "
                  "upon the face of the deep.",
             # KJV italic: "was" is supplied.
             marks=(UnitMark(kind="supplied", start=14, end=17),)),
        Unit(ref="Genesis 1:3", label="3",
             text="And God said, Let there be light: and there was light.",
             breaks_before=(UnitMark(kind="page", start=0, end=0, attrs={"page": 2}),)),
    ),
    metadata={"book": "Genesis", "chapter": 1},
)


def test_every_unit_node_quotes_its_unit_exactly() -> None:
    laid = layout(GEN_1)

    verses = [n for n in laid.nodes if n.depth == 1]
    assert [laid.full_text[n.char_start:n.char_end] for n in verses] == [
        u.text for u in GEN_1.units
    ]
    assert [n.metadata["ref"] for n in verses] == [u.ref for u in GEN_1.units]
    root = laid.nodes[0]
    assert (root.char_start, root.char_end) == (0, len(laid.full_text))


def test_labels_are_layout_not_part_of_the_unit() -> None:
    laid = layout(GEN_1)

    assert laid.full_text.startswith("1\tIn the beginning")
    first = laid.nodes[1]
    assert laid.full_text[first.char_start - 2:first.char_start] == "1\t"


def test_marks_land_on_what_they_mark() -> None:
    laid = layout(GEN_1)

    supplied = next(m for m in laid.marks if m.kind == "supplied")
    assert laid.full_text[supplied.char_start:supplied.char_end] == "was"
    page = next(m for m in laid.marks if m.kind == "page")
    assert page.char_start == page.char_end
    assert laid.full_text[page.char_start:].startswith("3\tAnd God said")


def test_windows_are_verbatim_and_carry_their_range() -> None:
    laid = layout(GEN_1)

    for passage in laid.passages:
        assert passage.text == laid.full_text[passage.char_start:passage.char_end]
    assert laid.passages[0].locator["start"] == "Genesis 1:1"
    assert laid.passages[-1].locator["end"] == "Genesis 1:3"


def test_small_budgets_never_split_a_unit() -> None:
    laid = layout(GEN_1, window_tokens=5)

    assert len(laid.passages) == 3
    assert [p.locator["ref"] for p in laid.passages] == [u.ref for u in GEN_1.units]


async def test_the_chunker_rebuilds_the_ingested_windows_from_text_alone() -> None:
    laid = layout(GEN_1, window_tokens=20)

    rebuilt = await UnitWindowChunker(window_tokens=20).chunk(laid.full_text)

    assert [(p.char_start, p.char_end) for p in rebuilt] == [
        (p.char_start, p.char_end) for p in laid.passages
    ]


async def test_the_chunker_meets_the_sdk_contract() -> None:
    await assert_chunker_contract(UnitWindowChunker())


def test_core_resolves_the_chunker_by_the_id_the_layout_stamps() -> None:
    laid = layout(GEN_1)

    assert {p.chunker for p in laid.passages} == {"unit_window"}
    assert isinstance(get_chunker("unit_window"), UnitWindowChunker)


@pytest.mark.parametrize(
    ("unit", "message"),
    [
        (Unit(ref="X 1", text="one\n\ntwo"), "blank line"),
        (Unit(ref="X 1", text=" padded"), "whitespace"),
        (Unit(ref="X 1", text="   "), "no text"),
        (Unit(ref="X 1", text="ok", label="1\t"), "label"),
        (Unit(ref="X 1", text="ok", marks=(UnitMark("supplied", 1, 9),)), "outside"),
    ],
)
def test_units_that_would_break_the_text_are_refused(unit: Unit, message: str) -> None:
    with pytest.raises(LayoutError, match=message):
        layout(UnitDocument(title="X", source="test:x", units=(unit,)))
