"""`find_lemma` against the corpus it was built for.

The numbers here are the survey's own: 422 occurrences of *mishpat* across 31
books, 157 of *tsedaqah* across 22. They are asserted exactly, because the whole
value of the tool is that it can be trusted to be exhaustive — a lemma lookup
that quietly returned 400 of 422 would be worse than none, since nothing
downstream could tell.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

from research_engine.services.words import LemmaLookup, LemmaQuery

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration]

MISHPAT = "4941"
TSEDAQAH = "6666"


@pytest.fixture
def lookup(engine: AsyncEngine) -> LemmaLookup:
    return LemmaLookup(engine)


class TestTheSurveyCounts:
    @pytest.mark.parametrize(
        ("strong", "occurrences", "books"),
        [(MISHPAT, 422, 31), (TSEDAQAH, 157, 22)],
    )
    async def test_a_lemma_is_found_exhaustively(
        self, lookup: LemmaLookup, strong: str, occurrences: int, books: int
    ) -> None:
        result = await lookup.find(LemmaQuery(strong=strong))
        assert (result.total, result.books) == (occurrences, books)
        assert len(result.occurrences) == occurrences

    async def test_the_counts_agree_with_the_occurrences_they_summarise(
        self, lookup: LemmaLookup
    ) -> None:
        """Aggregates run as their own queries, so they can drift from the list."""
        result = await lookup.find(LemmaQuery(strong=MISHPAT))
        for key in ("by_surface", "by_book", "by_morph", "by_prefixes"):
            assert sum(row["count"] for row in result.counts[key]) == result.total, key
        assert len(result.counts["by_book"]) == result.books


class TestOccurrencesAreCitable:
    """The constraint the whole design turns on."""

    async def test_no_occurrence_carries_a_character_span_or_a_document(
        self, lookup: LemmaLookup
    ) -> None:
        """A WLC span does not address the same characters in LHB.

        The index is built on WLC; the survey quotes LHB. Handing back an offset
        would produce addresses that fail to verify against the edition being
        quoted, which moves the manual work rather than removing it.
        """
        result = await lookup.find(LemmaQuery(strong=TSEDAQAH))
        forbidden = {"char_start", "char_end", "document_id", "position", "id"}
        for occurrence in result.occurrences:
            assert not (forbidden & set(occurrence)), occurrence

    async def test_every_occurrence_carries_a_verse_reference(
        self, lookup: LemmaLookup
    ) -> None:
        result = await lookup.find(LemmaQuery(strong=TSEDAQAH))
        for occurrence in result.occurrences:
            assert occurrence["ref"].count(".") == 2
            assert occurrence["ref"] == (
                f"{occurrence['book']}.{occurrence['chapter']}.{occurrence['verse']}"
            )

    async def test_every_occurrence_carries_its_morphology_and_prefixes(
        self, lookup: LemmaLookup
    ) -> None:
        result = await lookup.find(LemmaQuery(strong=MISHPAT))
        assert all(o["morph"] for o in result.occurrences)
        assert all("prefixes" in o for o in result.occurrences)
        assert any(o["prefixes"] for o in result.occurrences)

    async def test_the_prefixes_are_reported_rather_than_normalised_away(
        self, lookup: LemmaLookup
    ) -> None:
        """`k/4941` is an idiom, not noise: "according to the mishpat of".

        A lookup that stripped prefixes to compare bare lemmas would delete the
        finding, so the counts keep them and these two numbers are the evidence.
        """
        result = await lookup.find(LemmaQuery(strong=MISHPAT))
        by_prefix = {row["prefixes"]: row["count"] for row in result.counts["by_prefixes"]}
        assert by_prefix["k"] == 37
        assert by_prefix["b"] == 33

    async def test_occurrences_come_back_in_canonical_book_order(
        self, lookup: LemmaLookup
    ) -> None:
        """Ordered by `edition_books.ordinal`, so Genesis precedes Malachi."""
        result = await lookup.find(LemmaQuery(strong=TSEDAQAH))
        books = [row["book"] for row in result.counts["by_book"]]
        assert books[0] == "Gen"
        assert books.index("Deut") < books.index("Isa") < books.index("Mal")


class TestTheVersificationHop:
    async def test_an_occurrence_carries_the_english_reference_when_it_differs(
        self, lookup: LemmaLookup
    ) -> None:
        result = await lookup.find(LemmaQuery(strong=MISHPAT))
        mapped = [o for o in result.occurrences if o["english"]["mapping"] != "same"]
        assert mapped, "some mishpat occurrences sit in versification-divergent verses"
        for occurrence in mapped:
            assert occurrence["english"]["ref"] != occurrence["ref"]

    async def test_an_unmapped_verse_reports_the_same_reference_on_both_sides(
        self, lookup: LemmaLookup
    ) -> None:
        result = await lookup.find(LemmaQuery(strong=MISHPAT, book="Gen"))
        for occurrence in result.occurrences:
            assert occurrence["english"] == {
                "ref": occurrence["ref"],
                "mapping": "same",
                "part": None,
                "hebrew_part": None,
            }


class TestTheMapMustActuallyBeLoaded:
    """Migration 016 creates `core.verse_map`; a script fills it.

    The gap between those two steps used to be invisible: a verse the
    traditions agree on has no row, so an unloaded map was indistinguishable
    from universal agreement and every occurrence reported `same`.
    """

    async def test_this_corpus_has_its_map_loaded(
        self, lookup: LemmaLookup, engine: AsyncEngine
    ) -> None:
        async with engine.connect() as conn:
            assert await lookup.verse_map_is_loaded(conn) is True

    async def test_an_empty_map_yields_unmapped_and_says_so(
        self, lookup: LemmaLookup, engine: AsyncEngine
    ) -> None:
        """Emptied inside a transaction that is rolled back, never committed.

        The session-wide `corpus_is_unchanged` guard measures every table in
        `core`, so this would fail the whole run if it leaked.
        """
        conn = await engine.connect()
        trans = await conn.begin()
        try:
            await conn.execute(sa.text("DELETE FROM core.verse_map"))
            assert await lookup.verse_map_is_loaded(conn) is False
        finally:
            await trans.rollback()
            await conn.close()

        async with engine.connect() as check:
            remaining = (
                await check.execute(sa.text("SELECT count(*) FROM core.verse_map"))
            ).scalar()
        assert remaining == 1978, "the rollback must leave the corpus untouched"


class TestHomographs:
    """59,061 rows carry a letter Strong's does not have.

    An API keyed on the number alone would re-bake the conflation the index
    exists to escape, so the parameter has three states rather than two.
    """

    async def test_omitting_the_homograph_returns_every_homograph(
        self, lookup: LemmaLookup, engine: AsyncEngine
    ) -> None:
        async with engine.connect() as conn:
            strong, letters = (
                await conn.execute(
                    sa.text(
                        "SELECT strong, count(DISTINCT homograph) AS n FROM core.words "
                        "WHERE homograph IS NOT NULL GROUP BY 1 HAVING count(DISTINCT homograph) > 1 "
                        "ORDER BY n DESC, strong LIMIT 1"
                    )
                )
            ).one()
        assert letters > 1, "the corpus should hold a number OSHB split"

        every = await lookup.find(LemmaQuery(strong=strong, include_occurrences=False))
        split = [
            await lookup.find(
                LemmaQuery(strong=strong, homograph=row["homograph"], include_occurrences=False)
            )
            for row in every.counts["by_homograph"]
        ]
        assert sum(part.total for part in split) == every.total
        assert all(part.total < every.total for part in split)

    async def test_an_empty_homograph_means_only_the_rows_without_a_letter(
        self, lookup: LemmaLookup
    ) -> None:
        """A different question from "every row under this number"."""
        result = await lookup.find(LemmaQuery(strong=MISHPAT, homograph=""))
        assert result.total == 422
        assert all(o["homograph"] is None for o in result.occurrences)


class TestNarrowing:
    async def test_a_book_narrows_the_result(self, lookup: LemmaLookup) -> None:
        whole = await lookup.find(LemmaQuery(strong=MISHPAT, include_occurrences=False))
        one = await lookup.find(LemmaQuery(strong=MISHPAT, book="Ps"))
        assert 0 < one.total < whole.total
        assert {o["book"] for o in one.occurrences} == {"Ps"}

    async def test_a_chapter_range_narrows_within_a_book(
        self, lookup: LemmaLookup
    ) -> None:
        result = await lookup.find(
            LemmaQuery(strong=MISHPAT, book="Ps", chapter_start=1, chapter_end=50)
        )
        assert result.total > 0
        assert all(1 <= o["chapter"] <= 50 for o in result.occurrences)

    async def test_an_unknown_number_reports_nothing_and_says_so(
        self, lookup: LemmaLookup
    ) -> None:
        result = await lookup.find(LemmaQuery(strong="99999999"))
        assert (result.total, result.occurrences) == (0, [])
        assert result.notes and "99999999" in result.notes[0]

    async def test_a_greek_number_does_not_collide_with_a_hebrew_one(
        self, lookup: LemmaLookup
    ) -> None:
        """The reason the index is on `(language, strong)`, tested before it bites."""
        assert (await lookup.find(LemmaQuery(strong=MISHPAT, language="grc"))).total == 0
        assert (await lookup.find(LemmaQuery(strong=MISHPAT, language="he"))).total == 422


class TestTheTool:
    """The MCP surface, which is the only way an agent reaches any of this."""

    async def test_find_lemma_is_registered(self) -> None:
        from research_engine.mcp.dispatch import CORE_TOOL_MODULES

        assert "find_lemma" in {module.TOOL_NAME for module in CORE_TOOL_MODULES}

    async def _call(self, engine: AsyncEngine, **kwargs: Any) -> dict[str, Any]:
        from types import SimpleNamespace

        from research_engine.mcp.tools import find_lemma
        from research_engine.services.words import LemmaLookup

        container = SimpleNamespace(lemma_lookup=LemmaLookup(engine))
        return await find_lemma.handler(container, **kwargs)

    async def test_the_tool_returns_the_survey_counts(self, engine: AsyncEngine) -> None:
        result = await self._call(engine, strong=MISHPAT)
        assert (result["total"], result["books"]) == (422, 31)
        assert len(result["occurrences"]) == 422

    async def test_the_tool_accepts_the_form_printed_in_a_lexicon(
        self, engine: AsyncEngine
    ) -> None:
        """"H4941" is how every lexicon writes it; a confident zero would be worse."""
        result = await self._call(engine, strong="H4941")
        assert result["total"] == 422
        assert any("H4941" in note for note in result["notes"])

    async def test_a_lemma_string_is_refused_rather_than_guessed_at(
        self, engine: AsyncEngine
    ) -> None:
        result = await self._call(engine, strong="c/4941")
        assert result["error"]["code"] == "invalid_input"

    async def test_an_unknown_book_lists_the_ones_that_exist(
        self, engine: AsyncEngine
    ) -> None:
        result = await self._call(engine, strong=MISHPAT, book="Ecclesiastes")
        assert result["error"]["code"] == "unknown_book"
        assert "Eccl" in result["error"]["details"]["known_books"]

    async def test_a_chapter_range_arrives_as_a_two_element_array(
        self, engine: AsyncEngine
    ) -> None:
        result = await self._call(engine, strong=MISHPAT, book="Ps", chapters=[1, 50])
        assert all(1 <= o["chapter"] <= 50 for o in result["occurrences"])
        assert result["query"]["chapters"] == [1, 50]
