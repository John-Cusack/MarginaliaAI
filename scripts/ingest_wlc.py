"""Ingest the Westminster Leningrad Codex as a citable Hebrew edition.

Drives the existing `IngestionOrchestrator.ingest_drafts` rather than writing
rows itself, so the WLC chapters land the same way the Lexham Hebrew Bible
chapters did: one document per chapter, canonical text in `core.document_texts`,
`verse_boundary` passages with embeddings and FTS, and a `bibliography.editions`
row written inside the same transaction.

Idempotency is the orchestrator's, not ours: it hashes `full_text` and skips a
(content_hash, source) it already holds, before it embeds anything. A second run
therefore costs one query per chapter and writes nothing.

Needs a morphhb checkout; the edition metadata records whichever commit is
present, and the ingested corpus was built from `3d15126f`:

    git clone https://github.com/openscriptures/morphhb /tmp/morphhb

Usage (from the repository root):
    uv run python scripts/ingest_wlc.py --dry-run
    uv run python scripts/ingest_wlc.py --limit 3
    uv run python scripts/ingest_wlc.py
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import wlc_extract as W  # noqa: E402

EDITION_KEY = "WLC"
TITLE_PREFIX = "Westminster Leningrad Codex"
LHB_TITLE_PREFIX = "Lexham Hebrew Bible — "
RESOURCE_ID = "openscriptures/morphhb"
SOURCE_SCHEME = "openscriptures"
SOURCE_REPO = "https://github.com/openscriptures/morphhb"

#: Whose `document_type` this is not: WLC never went through Logos, so it does
#: not get `logos_book` even though every other field mirrors LHB. `generic` is
#: core's own default and carries no pack semantics; the edition identity lives
#: in `metadata.edition_key`, which is what citations key on.
DOCUMENT_TYPE = "generic"

#: LHB's Logos book codes to morphhb's OSIS filenames.
LHB_TO_OSIS = {
    "GE": "Gen", "EX": "Exod", "LE": "Lev", "NU": "Num", "DE": "Deut",
    "JOS": "Josh", "JDG": "Judg", "RU": "Ruth", "1SA": "1Sam", "2SA": "2Sam",
    "1KI": "1Kgs", "2KI": "2Kgs", "1CH": "1Chr", "2CH": "2Chr", "EZR": "Ezra",
    "NE": "Neh", "ES": "Esth", "JOB": "Job", "PS": "Ps", "PR": "Prov",
    "ECC": "Eccl", "SO": "Song", "IS": "Isa", "JE": "Jer", "LA": "Lam",
    "EZE": "Ezek", "DA": "Dan", "HO": "Hos", "JOE": "Joel", "AM": "Amos",
    "OB": "Obad", "JON": "Jonah", "MIC": "Mic", "NAH": "Nah", "HAB": "Hab",
    "ZEP": "Zeph", "HAG": "Hag", "ZEC": "Zech", "MAL": "Mal",
}


def morphhb_commit(repo: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


async def lhb_chapters(engine) -> dict[tuple[str, int], str]:
    """LHB's (book code, chapter) to its title's book-and-chapter suffix.

    The chapter list is derived from LHB rather than from the canon or from
    morphhb, because the point of this edition is to sit alongside LHB
    one-for-one. Anything LHB does not have is not a chapter this ingest wants.
    """
    import sqlalchemy as sa

    sql = sa.text(
        "SELECT metadata->>'article' AS article, title FROM core.documents "
        "WHERE metadata->>'edition_key' = 'LHB'"
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(sql)).fetchall()

    out: dict[tuple[str, int], str] = {}
    for article, title in rows:
        _, code, chapter = article.split(".")
        suffix = title.removeprefix(LHB_TITLE_PREFIX)
        # Obadiah is one chapter, so LHB's title carries no number. The WLC
        # titles are uniform `<Book> <Chapter>`, so give it the number back.
        if not re.search(r" \d+$", suffix):
            suffix = f"{suffix} {chapter}"
        out[(code, int(chapter))] = suffix
    return out


def build_metadata(
    code: str, chapter: int, osis_book: str, commit: str, kq: list[W.KQ]
) -> dict:
    """Document metadata: LHB's four keys, plus where this text came from."""
    return {
        "article": f"{EDITION_KEY}.{code}.{chapter}",
        "language": "he",
        "edition_key": EDITION_KEY,
        "resource_id": RESOURCE_ID,
        "osis_id": f"{osis_book}.{chapter}",
        "provenance": {
            "repo": SOURCE_REPO,
            "commit": commit,
            "file": f"wlc/{osis_book}.xml",
            "retrieved": date.today().isoformat(),
            "rights": "public domain (WLC text); morphhb markup CC-BY-4.0",
            "unicode_normalization": "NFC",
            # morphhb writes the ketiv unpointed and the qere pointed. A pointed
            # edition that goes unpointed 1,251 times is not one, so the qere is
            # what the canonical text carries — and the pairs stay here so the
            # reading the codex *writes* is still recoverable.
            "qere_policy": "qere-in-text",
        },
        "ketiv_qere": [
            {"verse": p.verse, "ketiv": p.ketiv, "qere": p.qere} for p in kq
        ],
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wlc-dir", type=Path, default=Path("/tmp/morphhb/wlc"))
    ap.add_argument("--repo", type=Path, default=Path("/tmp/morphhb"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.services.ingestion.pipeline import get_chunker

    commit = morphhb_commit(args.repo)
    chapters = {
        (c.book, c.number): c for c in W.load_all(args.wlc_dir)
    }
    print(f"morphhb {commit[:12]} — {len(chapters)} chapters parsed")

    container = await build_container(load_settings())
    # `verse_boundary` is the logos pack's `VerseChunker`, which is what LHB was
    # chunked with and therefore the only way these two editions stay
    # comparable. Resolving it by id through the plugin registry keeps this
    # script from naming a checkout path that exists on one machine; when the
    # pack is absent it raises `Unknown chunker: verse_boundary`, which is the
    # honest failure rather than a silent fallback to prose windows.
    chunker = get_chunker("verse_boundary")
    try:
        wanted = await lhb_chapters(container.engine)
        print(f"LHB gives {len(wanted)} chapters to mirror")

        existing = await container.ingestion.find_existing(
            source_pattern=f"{SOURCE_SCHEME}:{RESOURCE_ID}:"
        )
        print(f"already ingested: {len(existing)}")

        stats = {"ingested": 0, "skipped": 0, "passages": 0, "missing": 0}
        items = sorted(wanted.items(), key=lambda kv: (kv[0][0], kv[0][1]))
        if args.limit:
            items = items[: args.limit]

        for (code, chapter), suffix in items:
            osis_book = LHB_TO_OSIS[code]
            chap = chapters.get((osis_book, chapter))
            if chap is None:
                print(f"  MISSING in morphhb: {code}.{chapter}")
                stats["missing"] += 1
                continue

            # NFC is what makes this comparable to LHB at all: morphhb writes
            # dagesh before sheva and shin-dot before hiriq, which is not
            # canonical order. LHB is already canonically ordered, and the
            # quote matcher folds per character, so it cannot reorder marks
            # itself — the reordering has to happen here or never.
            full_text = unicodedata.normalize("NFC", W.render_chapter(chap))
            title = f"{TITLE_PREFIX} — {suffix}"
            source = f"{SOURCE_SCHEME}:{RESOURCE_ID}:{EDITION_KEY}.{code}.{chapter}"
            metadata = build_metadata(code, chapter, osis_book, commit, chap.kq)

            # Passages carry LHB's four keys and nothing else, so the two
            # editions' passage metadata line up; provenance stays on the
            # document rather than being copied into every chunk.
            passage_metadata = {
                k: metadata[k]
                for k in ("article", "language", "edition_key", "resource_id")
            }
            drafts = await chunker.chunk(full_text, passage_metadata)

            if args.dry_run:
                print(
                    f"  [dry] {title}: {len(full_text)} chars, "
                    f"{len(chap.verses)} verses, {len(drafts)} passages, "
                    f"{len(chap.kq)} k/q"
                )
                continue

            result = await container.ingestion.ingest_drafts(
                title=title,
                document_type=DOCUMENT_TYPE,
                passage_drafts=drafts,
                source=source,
                metadata=metadata,
                language="he",
                full_text=full_text,
            )
            if result.get("skipped") == "duplicate":
                stats["skipped"] += 1
            else:
                stats["ingested"] += 1
                stats["passages"] += result["passage_count"]
            done = stats["ingested"] + stats["skipped"]
            if done % 50 == 0:
                print(f"  {done}/{len(items)} … {stats}")

        print(f"\ndone: {stats}")
    finally:
        await container.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
