"""Find every occurrence of a lemma, and report it as a citable reference.

The constraint that shapes this whole module: **it returns verse references and
never character spans.**

`core.words` indexes the Westminster Leningrad Codex. The survey it exists to
serve quotes the Lexham Hebrew Bible — 52 citations to 7 — and although the two
editions agree on verse count in all 929 shared chapters, only about 47% of
those chapters are consonantally identical. A character span into WLC therefore
does not address the same characters in LHB. Handing one back would produce
addresses that fail to verify against the edition the author is actually
quoting, which relocates the manual work rather than removing it.

A verse reference survives the hop, because verse *identity* is what the two
editions share. The caller cites whichever edition they are quoting at that
reference, and `verify_quote` resolves the span there.

The second hop is versification. `core.words.ref` is in the Hebrew scheme, and
the English tradition puts 1,978 of those verses somewhere else — `Ps.36.6` in
Hebrew is `Ps.36.5` in English. Each occurrence therefore carries its English
reference too, taken from `core.verse_map`, and says which of the three cases it
is: the same verse, a mapped verse, or a *partial* — a verse beginning midway
through another, which has no single equivalent and is reported rather than
rounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger()

#: Refuse rather than truncate past this many occurrences. The two words the
#: index was built for return 422 and 157; a number an order of magnitude larger
#: is a request for a corpus dump, and the aggregates answer it better.
MAX_OCCURRENCES = 2_000


@dataclass(frozen=True)
class LemmaQuery:
    """What to look up. `strong` alone is ambiguous in two different ways.

    `language` disambiguates the lexicon — a Strong's number is unique only
    inside one, and H4941 and G4941 are different words. It defaults rather than
    being required because the column holds one language today, but it is always
    part of the query, so the index that matters is `(language, strong)`.

    `homograph` disambiguates the *word*. OSHB splits entries Strong's conflated
    and marks the halves with a letter: 59,061 rows carry one. Leaving it out of
    the API would re-bake the exact conflation this index exists to escape, so
    it is a first-class parameter with three states — absent (every homograph),
    a letter (that one), or the empty string (only rows with no letter).
    """

    strong: str
    language: str = "he"
    homograph: str | None = None
    book: str | None = None
    chapter_start: int | None = None
    chapter_end: int | None = None
    include_occurrences: bool = True


@dataclass
class LemmaResult:
    query: dict[str, Any]
    total: int
    books: int
    occurrences: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


#: The scheme pair every occurrence is reported against. `core.words.ref` is
#: written by the WLC ingest in the Masoretic numbering, and the reference a
#: caller quoting an English edition needs is the other side of this pair.
HEBREW, ENGLISH = "hebrew", "english"


def english_reference(
    ref: str,
    to_ref: str | None,
    to_part: str | None,
    from_part: str | None,
    mapping_type: str | None,
    *,
    map_loaded: bool,
) -> dict[str, Any]:
    """Render one occurrence's English-side reference.

    Split out and made pure because this is where two very different facts were
    being reported identically. A verse the traditions agree on has no row in
    `core.verse_map`, and so did *every* verse when the map had not been loaded
    — `mapping: "same"` in both cases. The second is not an answer, and looked
    exactly like one: migration 016 creates the tables but
    `scripts/load_versification.py` fills them, so a migrate without that step
    left `find_lemma` confidently wrong about 1,978 verses.

    Three outcomes now, and `unmapped` is not `same`:

    * ``unmapped`` — there is no map to consult. `ref` is None, because the
      honest answer is that this is unknown, not that it is unchanged.
    * ``same`` — the map is loaded and holds no row, so the traditions agree.
    * ``full`` / ``partial`` — the map says where the verse moved.
    """
    if not map_loaded:
        return {"ref": None, "mapping": "unmapped", "part": None, "hebrew_part": None}
    if to_ref is None:
        return {"ref": ref, "mapping": "same", "part": None, "hebrew_part": None}
    return {
        "ref": to_ref,
        "mapping": mapping_type,
        "part": to_part,
        "hebrew_part": from_part,
    }


class LemmaLookup:
    """Reads `core.words`, `core.verse_map` and `core.edition_books`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    def _where(q: LemmaQuery) -> tuple[str, dict[str, Any]]:
        """The shared predicate, so occurrences and aggregates cannot diverge."""
        clauses = ["w.language = :language", "w.strong = :strong"]
        params: dict[str, Any] = {"language": q.language, "strong": q.strong}

        if q.homograph == "":
            # Explicitly "the rows Strong's did not split", which is a different
            # question from "every row under this number".
            clauses.append("w.homograph IS NULL")
        elif q.homograph is not None:
            clauses.append("w.homograph = :homograph")
            params["homograph"] = q.homograph

        if q.book:
            clauses.append("split_part(w.ref, '.', 1) = :book")
            params["book"] = q.book
        if q.chapter_start is not None:
            clauses.append("split_part(w.ref, '.', 2)::int >= :chapter_start")
            params["chapter_start"] = q.chapter_start
        if q.chapter_end is not None:
            clauses.append("split_part(w.ref, '.', 2)::int <= :chapter_end")
            params["chapter_end"] = q.chapter_end
        return " AND ".join(clauses), params

    async def verse_map_is_loaded(self, conn: Any) -> bool:
        """Whether there is a Hebrew-to-English map to consult at all.

        Coarse on purpose: it separates "migrated but never loaded" — the
        failure that actually happens, and the one that used to be silent —
        from a working map. A partially loaded map is not detectable here and
        is the integration suite's job, which asserts the exact row count.
        """
        found = (
            await conn.execute(
                sa.text(
                    "SELECT 1 FROM core.verse_map "
                    "WHERE from_scheme = :src AND to_scheme = :dst LIMIT 1"
                ),
                {"src": HEBREW, "dst": ENGLISH},
            )
        ).first()
        return found is not None

    async def known_books(self, language: str = "he") -> list[str]:
        """The OSIS book ids `core.words` actually holds, in canonical order."""
        sql = sa.text(
            "SELECT DISTINCT split_part(w.ref, '.', 1) AS osis_id, "
            "       min(COALESCE(b.ordinal, 999)) AS ordinal "
            "FROM core.words w "
            "LEFT JOIN core.edition_books b "
            "       ON b.osis_id = split_part(w.ref, '.', 1) "
            "      AND b.edition_key = 'WLC' "
            "WHERE w.language = :language "
            "GROUP BY 1 ORDER BY 2, 1"
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sql, {"language": language})).fetchall()
        return [row[0] for row in rows]

    async def find(self, q: LemmaQuery) -> LemmaResult:
        where, params = self._where(q)
        notes: list[str] = []

        async with self._engine.connect() as conn:
            totals = (
                await conn.execute(
                    sa.text(
                        f"SELECT count(*) AS total, "  # noqa: S608 - clauses are literal
                        f"       count(DISTINCT split_part(w.ref, '.', 1)) AS books "
                        f"FROM core.words w WHERE {where}"
                    ),
                    params,
                )
            ).one()
            total, books = int(totals[0]), int(totals[1])

            result = LemmaResult(
                query={
                    "strong": q.strong,
                    "language": q.language,
                    "homograph": q.homograph,
                    "book": q.book,
                    "chapters": (
                        [q.chapter_start, q.chapter_end]
                        if (q.chapter_start is not None or q.chapter_end is not None)
                        else None
                    ),
                },
                total=total,
                books=books,
            )
            if total == 0:
                result.notes.append(
                    f"No word in {q.language!r} carries Strong's {q.strong}"
                    + (f" with homograph {q.homograph!r}" if q.homograph else "")
                    + ". Check the number, or drop the homograph to widen."
                )
                return result

            # Aggregates first: they describe the whole result set even when the
            # caller asked not to enumerate it.
            result.counts = await self._aggregates(conn, where, params)

            map_loaded = await self.verse_map_is_loaded(conn)
            if not map_loaded:
                # First in the list: every English reference below is withheld
                # because of this, and a caller that reads one note reads this.
                result.notes.append(
                    "core.verse_map is empty, so no English-tradition reference "
                    "could be resolved and every occurrence reports "
                    "mapping='unmapped'. The Hebrew references are unaffected "
                    "and remain citable in LHB and WLC. Run "
                    "`uv run python scripts/load_versification.py` to load the "
                    "1,978 mappings; migration 016 creates the tables but does "
                    "not fill them."
                )

            if not q.include_occurrences:
                return result
            if total > MAX_OCCURRENCES:
                result.notes.append(
                    f"{total} occurrences is over the {MAX_OCCURRENCES} limit; "
                    f"narrow with book or chapters, or read counts instead. "
                    f"No occurrences returned."
                )
                return result

            result.occurrences = await self._occurrences(
                conn, where, params, map_loaded=map_loaded
            )

        partials = [o for o in result.occurrences if o["english"]["mapping"] == "partial"]
        if partials:
            notes.append(
                f"{len(partials)} occurrence(s) sit in a verse the English "
                f"tradition splits or joins; their english.ref is the verse the "
                f"text begins in, and english.part says which half. Cite the "
                f"Hebrew reference unless you are quoting an English edition."
            )
        # `from_qere` is the one flag that predicts a *form* the target edition
        # may not print. WLC reads the qere; LHB prints the ketiv at these
        # references. Measured over both survey lemmas: of 579 occurrences, the
        # five whose surface is absent from the LHB verse are exactly the five
        # with from_qere true — no false positives and no misses. So the flag is
        # worth acting on rather than merely reporting.
        qere = [o for o in result.occurrences if o["from_qere"]]
        if qere:
            notes.append(
                f"{len(qere)} occurrence(s) come from a qere ({', '.join(o['ref'] for o in qere[:6])}"
                f"{', …' if len(qere) > 6 else ''}). The reference is right in "
                f"every edition, but an edition that prints the ketiv writes a "
                f"different word there — read the verse before quoting the "
                f"surface form."
            )
        result.notes.extend(notes)
        return result

    async def _occurrences(
        self, conn: Any, where: str, params: dict[str, Any], *, map_loaded: bool
    ) -> list[dict[str, Any]]:
        """One row per word, ordered canonically, carrying no span at all.

        The English reference is a LEFT JOIN, so a verse the two traditions
        agree on comes back with no mapping row — which is why `map_loaded` has
        to be passed in rather than inferred from the absence of a row. See
        `english_reference`. The join is on `from_ref` alone rather than on the
        part, so a partial produces one row per half and the caller sees that
        the verse is split instead of silently receiving whichever half sorted
        first.
        """
        sql = sa.text(
            f"""
            SELECT w.ref,
                   split_part(w.ref, '.', 1)        AS book,
                   split_part(w.ref, '.', 2)::int   AS chapter,
                   split_part(w.ref, '.', 3)::int   AS verse,
                   w.surface, w.lemma, w.morph, w.prefixes, w.homograph,
                   w.from_qere,
                   vm.to_ref, vm.to_part, vm.from_part, vm.mapping_type,
                   COALESCE(b.ordinal, 999)         AS ordinal
            FROM core.words w
            LEFT JOIN core.edition_books b
                   ON b.edition_key = 'WLC'
                  AND b.osis_id = split_part(w.ref, '.', 1)
            LEFT JOIN core.verse_map vm
                   ON vm.from_scheme = :from_scheme
                  AND vm.to_scheme = :to_scheme
                  AND vm.from_ref = w.ref
            WHERE {where}
            ORDER BY ordinal, chapter, verse, w.document_id, w.position,
                     vm.from_part NULLS FIRST
            """  # noqa: S608 - `where` is built from literal clauses only
        )
        rows = (
            await conn.execute(
                sql, {**params, "from_scheme": HEBREW, "to_scheme": ENGLISH}
            )
        ).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            english = english_reference(
                row.ref,
                row.to_ref,
                row.to_part,
                row.from_part,
                row.mapping_type,
                map_loaded=map_loaded,
            )
            occurrence = {
                # The Hebrew-scheme reference: what LHB and WLC both call this
                # verse, and what the caller cites.
                "ref": row.ref,
                "book": row.book,
                "chapter": row.chapter,
                "verse": row.verse,
                "english": english,
                "surface": row.surface,
                "lemma": row.lemma,
                "morph": row.morph,
                "prefixes": row.prefixes,
                "homograph": row.homograph,
                "from_qere": row.from_qere,
            }
            # A partial maps twice, once per half. Fold the halves onto the one
            # occurrence rather than duplicating the word.
            if out and out[-1]["ref"] == row.ref and out[-1]["surface"] == row.surface:
                previous = out[-1]
                if previous["english"]["mapping"] == "partial":
                    previous.setdefault("english_alternatives", []).append(english)
                    continue
            out.append(occurrence)
        return out

    async def _aggregates(
        self, conn: Any, where: str, params: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        """The counts a lexicographic survey actually reports.

        `prefixes` is counted, not normalised away: `k/4941` is 37 occurrences
        of "according to the *mishpat* of" and `b/4941` is 33. Those are
        findings about idiom, and a query that stripped them to compare bare
        lemmas would delete the result.
        """

        async def group(expression: str, label: str) -> list[dict[str, Any]]:
            sql = sa.text(
                f"SELECT {expression} AS value, count(*) AS n "  # noqa: S608
                f"FROM core.words w WHERE {where} "
                f"GROUP BY 1 ORDER BY n DESC, 1"
            )
            rows = (await conn.execute(sql, params)).fetchall()
            return [{label: row.value, "count": int(row.n)} for row in rows]

        by_book_sql = sa.text(
            f"""
            SELECT split_part(w.ref, '.', 1) AS value, count(*) AS n,
                   COALESCE(b.ordinal, 999) AS ordinal
            FROM core.words w
            LEFT JOIN core.edition_books b
                   ON b.edition_key = 'WLC'
                  AND b.osis_id = split_part(w.ref, '.', 1)
            WHERE {where}
            GROUP BY 1, 3 ORDER BY ordinal
            """  # noqa: S608
        )
        by_book = [
            {"book": row.value, "count": int(row.n)}
            for row in (await conn.execute(by_book_sql, params)).fetchall()
        ]

        return {
            "by_surface": await group("w.surface", "surface"),
            "by_book": by_book,
            "by_morph": await group("w.morph", "morph"),
            "by_prefixes": await group("COALESCE(w.prefixes, '')", "prefixes"),
            "by_homograph": await group("COALESCE(w.homograph, '')", "homograph"),
        }
