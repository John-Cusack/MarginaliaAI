"""Load versification schemes, book identity and the WLC-KJV verse map.

Three tables, all of them data that already existed somewhere and was being
re-derived, guessed at, or silently lost:

* `core.editions_versification` — which tradition each edition numbers by.
* `core.edition_books` — `(edition, article code) -> OSIS book id`, because LHB
  and ESV disagree about four Old Testament book codes and a join on the raw
  code returns silence rather than an error.
* `core.verse_map` — the 1,978 mappings in `wlc/VerseMap.xml`, which the WLC
  ingest read past and discarded.

Nothing is written until it validates. `--validate-only` runs every check and
writes nothing; `--dry-run` prints what would be written. The checks are the
point of the script, so they run on a real load too, and a failure refuses the
whole load rather than leaving a half-populated map.

Usage (from the repository root):
    uv run python scripts/load_versification.py --validate-only
    uv run python scripts/load_versification.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ingest_wlc import LHB_TO_OSIS  # noqa: E402

VERSEMAP_NS = "{http://www.APTBibleTools.com/namespace}"

#: LHB and WLC are laid out one-for-one and share LHB's article codes; ESV
#: differs on exactly four Old Testament books and adds the New Testament.
#: Recorded as an override on `LHB_TO_OSIS` rather than as a second full table,
#: so the four that actually differ are visible instead of buried in 39 rows.
ESV_OT_OVERRIDES = {"EC": "Eccl", "HOS": "Hos", "MI": "Mic", "NA": "Nah"}
ESV_OT_DROPPED = {"ECC", "HO", "MIC", "NAH"}

ESV_NT = {
    "MT": "Matt", "MK": "Mark", "LU": "Luke", "JN": "John", "AC": "Acts",
    "RO": "Rom", "1CO": "1Cor", "2CO": "2Cor", "GA": "Gal", "EPH": "Eph",
    "PHP": "Phil", "COL": "Col", "1TH": "1Thess", "2TH": "2Thess",
    "1TI": "1Tim", "2TI": "2Tim", "TIT": "Titus", "PHM": "Phlm", "HEB": "Heb",
    "JAM": "Jas", "1PE": "1Pet", "2PE": "2Pet", "1JN": "1John", "2JN": "2John",
    "3JN": "3John", "JUD": "Jude", "REV": "Rev",
}

#: Which tradition each edition numbers by. LHB and WLC are Hebrew editions and
#: number as the Masoretic text does; ESV follows the English tradition, which
#: is what `VerseMap.xml` calls KJV.
SCHEMES = {
    "LHB": ("hebrew", "Lexham Hebrew Bible; Masoretic verse numbering"),
    "WLC": ("hebrew", "Westminster Leningrad Codex; Masoretic verse numbering"),
    "ESV": ("english", "English Standard Version; KJV-tradition verse numbering"),
}


def edition_book_rows() -> list[dict]:
    """One row per `(edition, code)` the corpus actually addresses a book by."""
    rows = []
    # `LHB_TO_OSIS` is written in canonical order, so enumerate gives the
    # ordinal without a second list to keep in step with the first.
    for edition in ("LHB", "WLC"):
        for ordinal, (code, osis) in enumerate(LHB_TO_OSIS.items(), start=1):
            rows.append(
                {"edition_key": edition, "code": code, "osis_id": osis, "ordinal": ordinal}
            )

    esv = {c: o for c, o in LHB_TO_OSIS.items() if c not in ESV_OT_DROPPED}
    esv.update(ESV_OT_OVERRIDES)
    esv.update(ESV_NT)
    for ordinal, (code, osis) in enumerate(esv.items(), start=1):
        rows.append(
            {"edition_key": "ESV", "code": code, "osis_id": osis, "ordinal": ordinal}
        )
    return rows


def split_ref(raw: str) -> tuple[str, str | None]:
    """Split `"Isa.63.19!b"` into its reference and its half-verse part.

    The `!a`/`!b` suffix is how the source writes a verse that begins partway
    through another. It is kept as its own column rather than folded into the
    reference, because a reader that does not understand parts must be able to
    see that this row is not a whole-verse equivalence.
    """
    if "!" in raw:
        ref, part = raw.split("!", 1)
        return ref, part
    return raw, None


def parse_verse_map(path: Path, source: str) -> list[dict]:
    """Every mapping in `VerseMap.xml`, as rows.

    `wlc=` is the Hebrew-scheme reference and `kjv=` the English one. The file
    names its second side KJV; ESV follows the same tradition, which the
    validation below checks against the corpus rather than assuming.
    """
    root = ET.parse(path).getroot()
    rows = []
    for book in root.findall(f"{VERSEMAP_NS}book"):
        for verse in book.findall(f"{VERSEMAP_NS}verse"):
            from_ref, from_part = split_ref(verse.get("wlc") or "")
            to_ref, to_part = split_ref(verse.get("kjv") or "")
            mapping_type = verse.get("type") or "full"
            rows.append(
                {
                    "from_scheme": "hebrew",
                    "to_scheme": "english",
                    "from_ref": from_ref,
                    "to_ref": to_ref,
                    "from_part": from_part,
                    "to_part": to_part,
                    "mapping_type": mapping_type,
                    "source": source,
                }
            )
    return rows


def check_map_shape(rows: list[dict]) -> list[str]:
    """Structural checks on the map, independent of any corpus."""
    problems = []
    partials = [r for r in rows if r["mapping_type"] == "partial"]
    parted = [r for r in rows if r["from_part"] or r["to_part"]]

    for row in rows:
        if row["mapping_type"] not in ("full", "partial"):
            problems.append(f"unknown mapping type {row['mapping_type']!r}")
        if row["mapping_type"] == "full" and (row["from_part"] or row["to_part"]):
            problems.append(f"a 'full' mapping carries a part: {row['from_ref']}")
        for side in ("from_ref", "to_ref"):
            if len(row[side].split(".")) != 3:
                problems.append(f"{side} is not book.chapter.verse: {row[side]!r}")

    # Every partial should be marked as one. If a row carried an `!a` without
    # `type="partial"` the check constraint would reject the load; catching it
    # here says which row rather than which constraint.
    if {id(r) for r in partials} != {id(r) for r in parted}:
        problems.append(
            f"{len(partials)} rows typed partial but {len(parted)} carry a part"
        )

    seen = defaultdict(list)
    for row in rows:
        seen[(row["from_ref"], row["from_part"])].append(row["to_ref"])
    for key, targets in seen.items():
        if len(targets) > 1:
            problems.append(f"{key} maps to several references: {targets}")
    return problems


async def validate_against_corpus(engine, rows: list[dict], books: list[dict]) -> list[str]:
    """Check the map verse-by-verse against the corpus it will be used on.

    The map is WLC-to-KJV; this corpus holds ESV. Book- and chapter-level
    agreement had been checked before this script existed, verse-by-verse had
    not, and "the ESV follows the KJV tradition" is an assumption worth
    measuring rather than repeating.
    """
    import sqlalchemy as sa

    problems = []
    osis_of = {(b["edition_key"], b["code"]): b["osis_id"] for b in books}

    sql = sa.text(
        "SELECT d.metadata->>'edition_key' AS ed, "
        "       split_part(d.metadata->>'article', '.', 2) AS code, "
        "       (n.metadata->>'chapter')::int AS chapter, "
        "       (n.metadata->>'verse')::int AS verse "
        "FROM core.documents d "
        "JOIN core.document_nodes n ON n.document_id = d.id "
        "WHERE n.node_type = 'verse' "
        "  AND d.metadata->>'edition_key' IN ('LHB', 'WLC', 'ESV')"
    )
    async with engine.connect() as conn:
        rows_db = (await conn.execute(sql)).fetchall()

    present: dict[str, set[str]] = defaultdict(set)
    for edition, code, chapter, verse in rows_db:
        osis = osis_of.get((edition, code))
        if osis is None:
            problems.append(f"{edition} book code {code!r} has no OSIS id")
            continue
        present[edition].add(f"{osis}.{chapter}.{verse}")

    # 1. Every Hebrew side of the map must exist in both Hebrew editions.
    for edition in ("WLC", "LHB"):
        missing = sorted(
            {r["from_ref"] for r in rows} - present[edition]
        )
        if missing:
            problems.append(
                f"{len(missing)} mapped Hebrew references absent from {edition}: "
                f"{missing[:5]}"
            )

    # 2. Every English side must exist in ESV. This is the assumption under
    #    test: a KJV map is only usable here if its targets are ESV verses.
    missing_esv = sorted({r["to_ref"] for r in rows} - present["ESV"])
    if missing_esv:
        problems.append(
            f"{len(missing_esv)} mapped English references absent from ESV: "
            f"{missing_esv[:8]}"
        )

    # 3. The completeness check the brief names: repairing book identity must
    #    surface exactly the 27 books VerseMap knows about.
    mapped_books = {r["from_ref"].split(".")[0] for r in rows}
    divergent = set()
    heb_by_chapter: dict[tuple[str, int], int] = defaultdict(int)
    eng_by_chapter: dict[tuple[str, int], int] = defaultdict(int)
    for ref in present["LHB"]:
        book, chapter, _ = ref.split(".")
        heb_by_chapter[(book, int(chapter))] += 1
    for ref in present["ESV"]:
        book, chapter, _ = ref.split(".")
        eng_by_chapter[(book, int(chapter))] += 1
    for key, count in heb_by_chapter.items():
        if eng_by_chapter.get(key, -1) != count:
            divergent.add(key[0])
    if divergent != mapped_books:
        problems.append(
            f"divergent books {sorted(divergent)} != VerseMap books "
            f"{sorted(mapped_books)}"
        )
    return problems


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verse-map", type=Path, default=Path("/tmp/morphhb/wlc/VerseMap.xml"))
    ap.add_argument("--source", default=None, help="provenance string for the rows")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args()

    import sqlalchemy as sa

    from research_engine.adapters.storage.postgres.schema import (
        edition_books,
        editions_versification,
        verse_map,
    )
    from research_engine.composition import build_container
    from research_engine.config import load_settings

    source = args.source
    if source is None:
        from ingest_wlc import morphhb_commit

        commit = morphhb_commit(args.verse_map.parent.parent)
        source = f"morphhb@{commit[:7]} wlc/VerseMap.xml"

    books = edition_book_rows()
    mappings = parse_verse_map(args.verse_map, source)
    print(f"{len(books)} book codes, {len(mappings)} verse mappings from {source}")
    partials = [m for m in mappings if m["mapping_type"] == "partial"]
    print(
        f"  {len(mappings) - len(partials)} full, {len(partials)} partial, "
        f"{len({m['from_ref'].split('.')[0] for m in mappings})} books"
    )

    problems = check_map_shape(mappings)
    if problems:
        print("REFUSED — the map does not validate:")
        for p in problems[:10]:
            print(f"  {p}")
        return 1

    container = await build_container(load_settings())
    try:
        corpus_problems = await validate_against_corpus(container.engine, mappings, books)
        if corpus_problems:
            print("REFUSED — the map does not agree with the corpus:")
            for p in corpus_problems[:10]:
                print(f"  {p}")
            return 1
        print("validated: shape and corpus agreement both clean")

        if args.validate_only or args.dry_run:
            print("nothing written")
            return 0

        scheme_rows = [
            {"edition_key": key, "scheme": scheme, "notes": notes}
            for key, (scheme, notes) in SCHEMES.items()
        ]
        # Every edition in `bibliography.editions` must get a scheme, so an
        # edition added since this script was written fails loudly here rather
        # than silently defaulting to somebody's tradition.
        async with container.engine.connect() as conn:
            known = {
                row[0]
                for row in (
                    await conn.execute(sa.text("SELECT edition_key FROM bibliography.editions"))
                ).fetchall()
            }
        unscheduled = known - set(SCHEMES)
        if unscheduled:
            print(f"REFUSED — editions with no scheme declared: {sorted(unscheduled)}")
            return 1

        async with container.engine.begin() as conn:
            await conn.execute(sa.delete(verse_map).where(verse_map.c.source == source))
            await conn.execute(sa.delete(edition_books))
            await conn.execute(sa.delete(editions_versification))
            await conn.execute(editions_versification.insert(), scheme_rows)
            await conn.execute(edition_books.insert(), books)
            await conn.execute(verse_map.insert(), mappings)

        print(
            f"wrote {len(scheme_rows)} schemes, {len(books)} book codes, "
            f"{len(mappings)} mappings"
        )
        return 0
    finally:
        await container.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
