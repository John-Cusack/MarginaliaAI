"""Versification and book identity, asserted against the corpus that uses them.

These are read-only checks over the live corpus. They are integration tests
rather than fixtures because every one of them is a claim about *this* library's
data — that four book codes disagree, that 27 books diverge, that five specific
verses map where a hand-written survey says they map. A fixture would prove the
SQL runs; only the corpus proves the answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration]

#: Verse nodes of the three biblical editions, keyed by an OSIS reference every
#: edition can be addressed by. This is the join the book-code defect used to
#: break: `b.code = split_part(article, '.', 2)` is what turns LHB's "ECC" and
#: ESV's "EC" into the same book.
VERSE_NODES = """
SELECT d.metadata->>'edition_key' AS edition_key,
       b.osis_id,
       (n.metadata->>'chapter')::int AS chapter,
       (n.metadata->>'verse')::int   AS verse,
       b.osis_id || '.' || (n.metadata->>'chapter') || '.' || (n.metadata->>'verse') AS ref
FROM core.documents d
JOIN core.edition_books b
      ON b.edition_key = d.metadata->>'edition_key'
     AND b.code = split_part(d.metadata->>'article', '.', 2)
JOIN core.document_nodes n
      ON n.document_id = d.id AND n.node_type = 'verse'
"""


async def _scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(sql), params)).scalar()


async def _rows(engine: AsyncEngine, sql: str, **params: object) -> list:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(sql), params)).fetchall()


class TestSchemesCoverEveryEdition:
    async def test_every_edition_names_a_versification_scheme(
        self, engine: AsyncEngine
    ) -> None:
        """An edition with no declared scheme is one whose verses cannot be mapped."""
        missing = await _rows(
            engine,
            "SELECT e.edition_key FROM bibliography.editions e "
            "LEFT JOIN core.editions_versification v ON v.edition_key = e.edition_key "
            "WHERE v.edition_key IS NULL",
        )
        assert missing == [], (
            f"editions with no versification scheme: {[r[0] for r in missing]}. "
            f"Add them to SCHEMES in scripts/load_versification.py and reload."
        )

    async def test_the_schemes_are_the_two_traditions_the_corpus_holds(
        self, engine: AsyncEngine
    ) -> None:
        rows = await _rows(
            engine,
            "SELECT edition_key, scheme FROM core.editions_versification ORDER BY 1",
        )
        assert dict(rows) == {"ESV": "english", "LHB": "hebrew", "WLC": "hebrew"}


class TestTheVerseMap:
    """`wlc/VerseMap.xml` as loaded, and the shapes that break a naive reader."""

    async def test_every_mapping_in_the_source_is_loaded(
        self, engine: AsyncEngine
    ) -> None:
        assert await _scalar(engine, "SELECT count(*) FROM core.verse_map") == 1978

    async def test_seven_mappings_are_partial_and_carry_their_half(
        self, engine: AsyncEngine
    ) -> None:
        """A partial cannot be an integer offset, so it is stored as a pair.

        `Isa.63.19!b -> Isa.64.1` is a verse beginning midway through another.
        A schema with an `offset` column would have had to round these seven
        silently; here they are visibly not whole-verse equivalences.
        """
        partials = await _rows(
            engine,
            "SELECT from_ref, from_part, to_ref, to_part FROM core.verse_map "
            "WHERE mapping_type = 'partial' ORDER BY from_ref, from_part",
        )
        assert len(partials) == 7
        assert all(row.from_part or row.to_part for row in partials)
        assert ("Isa.63.19", "b", "Isa.64.1", None) in [tuple(r) for r in partials]

    async def test_a_full_mapping_never_carries_a_half(
        self, engine: AsyncEngine
    ) -> None:
        assert (
            await _scalar(
                engine,
                "SELECT count(*) FROM core.verse_map WHERE mapping_type = 'full' "
                "AND (from_part IS NOT NULL OR to_part IS NOT NULL)",
            )
            == 0
        )

    @pytest.mark.parametrize(
        ("hebrew", "english"),
        [
            ("Hos.2.21", "Hos.2.19"),
            ("Isa.9.6", "Isa.9.7"),
            ("Jer.9.23", "Jer.9.24"),
            ("Ps.36.6", "Ps.36.5"),
            ("Ps.89.15", "Ps.89.14"),
        ],
    )
    async def test_the_verses_the_survey_annotated_by_hand_resolve_from_data(
        self, engine: AsyncEngine, hebrew: str, english: str
    ) -> None:
        """W-001 carries these five as manual notes. They are now a lookup."""
        got = await _scalar(
            engine,
            "SELECT to_ref FROM core.verse_map WHERE from_scheme = 'hebrew' "
            "AND to_scheme = 'english' AND from_ref = :ref",
            ref=hebrew,
        )
        assert got == english

    async def test_every_mapped_hebrew_reference_exists_in_both_hebrew_editions(
        self, engine: AsyncEngine
    ) -> None:
        for edition in ("WLC", "LHB"):
            missing = await _scalar(
                engine,
                f"WITH v AS ({VERSE_NODES}) "  # noqa: S608 - literal
                "SELECT count(*) FROM ("
                "  SELECT DISTINCT from_ref FROM core.verse_map"
                ") m LEFT JOIN v ON v.edition_key = :ed AND v.ref = m.from_ref "
                "WHERE v.ref IS NULL",
                ed=edition,
            )
            assert missing == 0, f"{missing} mapped references absent from {edition}"

    async def test_every_mapped_english_reference_exists_in_esv(
        self, engine: AsyncEngine
    ) -> None:
        """The map is WLC-to-KJV; this corpus holds ESV.

        That the two traditions agree verse-for-verse is the assumption the map
        rests on here, and it is measured rather than asserted in a comment.
        """
        missing = await _scalar(
            engine,
            f"WITH v AS ({VERSE_NODES}) "  # noqa: S608 - literal
            "SELECT count(*) FROM (SELECT DISTINCT to_ref FROM core.verse_map) m "
            "LEFT JOIN v ON v.edition_key = 'ESV' AND v.ref = m.to_ref "
            "WHERE v.ref IS NULL",
        )
        assert missing == 0


class TestBookIdentity:
    """Four codes out of thirty-nine, and the silence they used to cause."""

    async def test_the_four_disputed_books_join_across_all_three_editions(
        self, engine: AsyncEngine
    ) -> None:
        """LHB writes `ECC HO MIC NAH`; ESV writes `EC HOS MI NA`.

        A join on the raw code returned zero rows for these four and reported
        nothing wrong, which is the failure mode worth a test: not an error, a
        confident empty answer.
        """
        rows = await _rows(
            engine,
            f"""
            WITH v AS ({VERSE_NODES})
            SELECT l.osis_id, count(*) AS n
            FROM v l
            JOIN v w ON w.edition_key = 'WLC' AND w.ref = l.ref
            JOIN v e ON e.edition_key = 'ESV'
                    AND e.ref = COALESCE(
                          (SELECT vm.to_ref FROM core.verse_map vm
                            WHERE vm.from_scheme = 'hebrew' AND vm.to_scheme = 'english'
                              AND vm.from_ref = l.ref AND vm.mapping_type = 'full'),
                          l.ref)
            WHERE l.edition_key = 'LHB'
              AND l.osis_id IN ('Eccl', 'Hos', 'Mic', 'Nah')
            GROUP BY 1 ORDER BY 1
            """,  # noqa: S608 - literal
        )
        joined = {row.osis_id: row.n for row in rows}
        assert set(joined) == {"Eccl", "Hos", "Mic", "Nah"}
        assert all(n > 0 for n in joined.values()), joined

    async def test_repairing_the_codes_surfaces_exactly_the_books_versemap_maps(
        self, engine: AsyncEngine
    ) -> None:
        """The check that the repair is complete, and it is exact.

        Joined on the raw article code, 23 books show diverging verse counts.
        Joined through `edition_books`, 27 do — and `VerseMap.xml` maps 27. The
        four that appear are the four whose codes never matched, so the
        versification defect had been hiding inside the book-identity one.
        """
        divergent = await _scalar(
            engine,
            f"""
            WITH v AS ({VERSE_NODES}),
            counts AS (
              SELECT edition_key, osis_id, chapter, count(*) AS n
              FROM v WHERE edition_key IN ('LHB', 'ESV') GROUP BY 1, 2, 3
            )
            SELECT count(DISTINCT l.osis_id)
            FROM counts l JOIN counts e USING (osis_id, chapter)
            WHERE l.edition_key = 'LHB' AND e.edition_key = 'ESV' AND l.n <> e.n
            """,  # noqa: S608 - literal
        )
        mapped_books = await _scalar(
            engine,
            "SELECT count(DISTINCT split_part(from_ref, '.', 1)) FROM core.verse_map",
        )
        assert mapped_books == 27
        assert divergent == mapped_books, (
            f"{divergent} books diverge but VerseMap maps {mapped_books}. "
            f"A mismatch means the book-code repair is incomplete."
        )

    async def test_hebrew_joel_has_a_chapter_english_joel_does_not(
        self, engine: AsyncEngine
    ) -> None:
        """The divergence that is not about verse numbers at all.

        Hebrew Joel runs to four chapters and English Joel to three, so Joel 4
        is the one Hebrew chapter with no ESV counterpart to join to. The map
        covers it (`Joel.4.1 -> Joel.3.1`), which is why the map has to be data
        rather than a per-chapter verse offset.
        """
        heb = await _scalar(
            engine,
            f"WITH v AS ({VERSE_NODES}) "  # noqa: S608 - literal
            "SELECT count(DISTINCT chapter) FROM v "
            "WHERE edition_key = 'LHB' AND osis_id = 'Joel'",
        )
        eng = await _scalar(
            engine,
            f"WITH v AS ({VERSE_NODES}) "  # noqa: S608 - literal
            "SELECT count(DISTINCT chapter) FROM v "
            "WHERE edition_key = 'ESV' AND osis_id = 'Joel'",
        )
        assert (heb, eng) == (4, 3)
        assert (
            await _scalar(
                engine,
                "SELECT to_ref FROM core.verse_map WHERE from_ref = 'Joel.4.1'",
            )
            == "Joel.3.1"
        )

    async def test_every_book_code_in_the_corpus_has_an_osis_id(
        self, engine: AsyncEngine
    ) -> None:
        """A code with no row is a book that silently drops out of every join."""
        orphans = await _rows(
            engine,
            "SELECT DISTINCT d.metadata->>'edition_key' AS ed, "
            "       split_part(d.metadata->>'article', '.', 2) AS code "
            "FROM core.documents d "
            "LEFT JOIN core.edition_books b "
            "       ON b.edition_key = d.metadata->>'edition_key' "
            "      AND b.code = split_part(d.metadata->>'article', '.', 2) "
            "WHERE d.metadata->>'edition_key' IN ('LHB', 'WLC', 'ESV') "
            "  AND b.code IS NULL",
        )
        assert orphans == [], f"book codes with no OSIS id: {[tuple(r) for r in orphans]}"
