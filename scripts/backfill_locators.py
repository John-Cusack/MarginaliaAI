"""Attach verse locators to the WLC passages already in the corpus.

`verify-quote` on a WLC hit reports "locator: none recorded for this document",
which reads as *this edition has no verses* rather than *nobody wrote them down*.
The verses were always derivable: the chapter text is rendered from morphhb by
`wlc_extract`, so every verse's character span is known, and a passage's span is
already stored. Intersecting the two says which verses a passage quotes.

This drives `PGPassageRepo.set_locators`, which exists for exactly this case — a
locator comes from the source rather than the text, so learning one later leaves
the chunk, its offsets and its embedding untouched. Re-ingesting to add a JSON
column would re-embed 4,772 passages to no purpose.

Locator shape follows the corpus's own range convention (`page_start`/`page_end`
on the Logos documents), with the human-readable `ref` that the two Leviticus 25
documents already use, so a single-verse WLC locator prints exactly like theirs:

    {"ref": "Genesis 1:1-7", "book": "Genesis", "chapter": 1,
     "verse_start": 1, "verse_end": 7}

Usage (from the repository root):
    uv run python scripts/backfill_locators.py --dry-run
    uv run python scripts/backfill_locators.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import wlc_extract as W  # noqa: E402
from ingest_wlc import EDITION_KEY, LHB_TO_OSIS, TITLE_PREFIX  # noqa: E402

OSIS_FOR = LHB_TO_OSIS


def verses_in_span(
    text: str, spans: list[tuple[int, int, int]], start: int, end: int
) -> list[int]:
    """Verse numbers a passage's character span actually quotes.

    Overlap has to be *visible* text: the chunker widens a span backwards for
    overlap and trims it to non-whitespace, so a boundary can clip a neighbouring
    verse by a character or two of nothing. Citing a verse a passage does not
    show would be worse than citing one fewer.
    """
    hit = []
    for verse, v_start, v_end in spans:
        lo, hi = max(v_start, start), min(v_end, end)
        if lo < hi and text[lo:hi].strip():
            hit.append(verse)
    return hit


def build_locator(book: str, chapter: int, verses: list[int]) -> dict:
    first, last = verses[0], verses[-1]
    ref = f"{book} {chapter}:{first}"
    if last != first:
        ref += f"-{last}"
    return {
        "ref": ref,
        "book": book,
        "chapter": chapter,
        "verse_start": first,
        "verse_end": last,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wlc-dir", type=Path, default=Path("/tmp/morphhb/wlc"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import sqlalchemy as sa

    from research_engine.composition import build_container
    from research_engine.config import load_settings

    osis_to_code = {v: k for k, v in OSIS_FOR.items()}
    chapters = {
        f"{osis_to_code[c.book]}.{c.number}": c for c in W.load_all(args.wlc_dir)
    }

    container = await build_container(load_settings())
    try:
        sql = sa.text(
            "SELECT d.id, d.title, d.metadata->>'article' AS article, "
            "       md5(t.text) AS digest "
            "FROM core.documents d JOIN core.document_texts t ON t.document_id = d.id "
            "WHERE d.metadata->>'edition_key' = :key ORDER BY d.title"
        )
        async with container.engine.connect() as conn:
            docs = (await conn.execute(sql, {"key": EDITION_KEY})).fetchall()
        print(f"{len(docs)} {EDITION_KEY} documents")

        updates: list[tuple] = []
        stats = {"docs": 0, "passages": 0, "single_verse": 0, "max_verses": 0}
        drift: list[str] = []

        for doc_id, title, article, digest in docs:
            key = article.split(".", 1)[1]
            chapter = chapters[key]
            text, spans = W.render_chapter_with_spans(chapter)
            text = unicodedata.normalize("NFC", text)

            # Refuse to derive locators from a rendering that is not the one
            # stored. If these ever disagree the offsets are meaningless, and a
            # confidently wrong verse number is worse than none.
            if hashlib.md5(text.encode()).hexdigest() != digest:
                drift.append(title)
                continue

            suffix = title.removeprefix(f"{TITLE_PREFIX} — ")
            match = re.match(r"^(.*) (\d+)$", suffix)
            assert match, f"unparseable title: {title}"
            book, chapter_no = match.group(1), int(match.group(2))

            for passage in await container.passages.get_by_document(doc_id):
                verses = verses_in_span(
                    text, spans, passage.char_start, passage.char_end
                )
                if not verses:
                    drift.append(f"{title} passage {passage.position}: no verses")
                    continue
                updates.append((passage.id, build_locator(book, chapter_no, verses)))
                stats["passages"] += 1
                stats["single_verse"] += len(verses) == 1
                stats["max_verses"] = max(stats["max_verses"], len(verses))
            stats["docs"] += 1

        print(f"prepared {len(updates)} locators over {stats['docs']} documents")
        print(f"  single-verse passages: {stats['single_verse']}")
        print(f"  widest passage spans {stats['max_verses']} verses")
        if drift:
            print(f"  SKIPPED {len(drift)}: {drift[:5]}")

        if args.dry_run:
            for _pid, loc in updates[:5]:
                print(f"  [dry] {loc}")
            return 1 if drift else 0

        written = await container.passages.set_locators(updates)
        print(f"set_locators wrote {written} rows")
        return 1 if drift else 0
    finally:
        await container.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
