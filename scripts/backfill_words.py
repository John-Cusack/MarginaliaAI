"""Load the Westminster Leningrad Codex's morphology into `core.words`.

The analysis has been in the source all along — every one of morphhb's `<w>`
elements carries a Strong's number and a parsing code — and the WLC ingest threw
it away, because `_word_text` reads character data and attributes are invisible
to it. This puts it back, addressed against the very same canonical text the
ingest produced, so a word joins to the passage that retrieved it and to the
verse node that contains it without anything new having to be agreed on.

Nothing here is trusted. A chapter is written only if all three hold:

* the text this script renders is byte-identical to the text the corpus stores
  (md5), so the offsets address the string they will be stored against;
* every word's span quotes that word exactly;
* no stretch of text left unclaimed by any word contains a Hebrew letter.

The third is the one that answers "do I have all the words". A count can agree
with a mistake — two words merged and one dropped counts the same as two words
right. A gap cannot: a word missing from the index leaves its letters lying in
text nothing claims, and the check reads them back at you.

Usage (from the repository root):
    uv run python scripts/backfill_words.py --dry-run
    uv run python scripts/backfill_words.py
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
from ingest_wlc import EDITION_KEY, LHB_TO_OSIS  # noqa: E402

LANGUAGE = "he"

#: A lemma's head: the Strong's number, optionally an OSHB homograph letter that
#: splits a number Strong's conflated, optionally a `+` marking a compound name.
_HEAD = re.compile(r"(\d+)\s*([a-z])?\+?$")


def parse_lemma(lemma: str) -> tuple[str | None, str | None, str | None]:
    """Split morphhb's compound lemma into Strong's number, homograph, prefixes.

    `"c/d/4941"` is the conjunction, the article and Strong's 4941 — one word in
    the text, three morphemes in the analysis. The number is what a survey
    queries; the prefixes are what it reads, since `k/4941` (37 occurrences,
    "according to the *mishpat* of") is an idiom and not noise to be normalised
    away. A head that is not a number at all — a bare preposition standing as
    its own word — yields no Strong's, which is why the column is nullable.
    """
    parts = lemma.split("/")
    prefixes = "/".join(parts[:-1]) or None
    match = _HEAD.match(parts[-1].strip())
    if not match:
        return None, None, prefixes
    return match.group(1), match.group(2), prefixes


def rows_for(document_id, text: str, words: list, ref_book: str, chapter: int) -> list[dict]:
    out = []
    for position, word in enumerate(words):
        strong, homograph, prefixes = parse_lemma(word.lemma)
        out.append(
            {
                "document_id": document_id,
                "position": position,
                "char_start": word.offset,
                "char_end": word.offset + word.length,
                "surface": word.surface,
                "lemma": word.lemma,
                "strong": strong,
                "homograph": homograph,
                "prefixes": prefixes,
                "morph": word.morph,
                "language": LANGUAGE,
                "ref": f"{ref_book}.{chapter}.{word.verse}",
                "from_qere": word.qere,
            }
        )
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wlc-dir", type=Path, default=Path("/tmp/morphhb/wlc"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import sqlalchemy as sa

    from research_engine.adapters.storage.postgres.schema import words as words_table
    from research_engine.composition import build_container
    from research_engine.config import load_settings

    osis_to_code = {v: k for k, v in LHB_TO_OSIS.items()}
    chapters = {
        f"{osis_to_code[c.book]}.{c.number}": c for c in W.load_all(args.wlc_dir)
    }

    container = await build_container(load_settings())
    try:
        sql = sa.text(
            "SELECT d.id, d.title, d.metadata->>'article' AS article, "
            "       d.metadata->>'osis_id' AS osis, md5(t.text) AS digest "
            "FROM core.documents d JOIN core.document_texts t ON t.document_id = d.id "
            "WHERE d.metadata->>'edition_key' = :k ORDER BY d.title"
        )
        stats = {"written": 0, "skipped": 0, "rows": 0, "qere": 0, "no_strong": 0}
        refused: list[tuple[str, str]] = []

        # One read connection for the whole pass: the per-document comparison
        # below runs 929 times, and reopening for each would dominate the run.
        async with container.engine.connect() as conn:
          docs = (await conn.execute(sql, {"k": EDITION_KEY})).fetchall()
          print(f"{len(docs)} {EDITION_KEY} documents")

          for doc_id, title, article, osis, digest in docs:
              code_chapter = article.split(".", 1)[1]
              chapter_obj = chapters[code_chapter]
              text, found, marks = W.render_chapter_with_words(chapter_obj)
              text = unicodedata.normalize("NFC", text)
              for word in found:
                  word.surface = unicodedata.normalize("NFC", word.surface)

              if hashlib.md5(text.encode()).hexdigest() != digest:
                  refused.append((title, "rendered text differs from the stored text"))
                  continue
              misplaced = W.misplaced_words(text, found)
              if misplaced:
                  refused.append((title, f"span does not quote its word: {misplaced[0]}"))
                  continue
              unclaimed = W.unclaimed_letters(text, found, marks)
              if unclaimed:
                  refused.append((title, f"unindexed letters: {unclaimed[:2]}"))
                  continue

              book = (osis or chapter_obj.book).split(".")[0]
              rows = rows_for(doc_id, text, found, book, chapter_obj.number)

              existing = (
                  await conn.execute(
                      sa.select(
                          words_table.c.position,
                          words_table.c.char_start,
                          words_table.c.char_end,
                          words_table.c.lemma,
                      )
                      .where(words_table.c.document_id == doc_id)
                      .order_by(words_table.c.position)
                  )
              ).all()
              unchanged = len(existing) == len(rows) and all(
                  e.position == r["position"]
                  and e.char_start == r["char_start"]
                  and e.char_end == r["char_end"]
                  and e.lemma == r["lemma"]
                  for e, r in zip(existing, rows, strict=True)
              )

              stats["rows"] += len(rows)
              stats["qere"] += sum(1 for r in rows if r["from_qere"])
              stats["no_strong"] += sum(1 for r in rows if r["strong"] is None)

              if unchanged or args.dry_run:
                  stats["skipped" if unchanged else "written"] += 1
                  continue

              async with container.engine.begin() as write:
                  await write.execute(
                      words_table.delete().where(words_table.c.document_id == doc_id)
                  )
                  await write.execute(words_table.insert(), rows)
              stats["written"] += 1
              done = stats["written"] + stats["skipped"]
              if done % 250 == 0:
                  print(f"  {done}/{len(docs)} …")

        print(f"\nwords: {stats['rows']:,}  (from a qere {stats['qere']:,}, "
              f"without a Strong's number {stats['no_strong']:,})")
        print(f"chapters written {stats['written']}, unchanged {stats['skipped']}")
        if refused:
            print(f"REFUSED {len(refused)}:")
            for title, why in refused[:8]:
                print(f"  {title}: {why}")
        return 1 if refused else 0
    finally:
        await container.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
