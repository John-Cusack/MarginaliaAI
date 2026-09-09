"""Give the LHB and ESV chapters the verse locators and structure WLC already has.

WLC is currently the only Bible in this corpus that knows where its own verses
are. Its 4,772 passages carry locators and sit under 23,213 verse nodes, so a
quotation from it verifies to `Genesis 18:19`. The other two editions — 7,809
passages between them — carry `{}` and no structure at all, so the same quote
from the Lexham Hebrew Bible reports only the chunk it was retrieved from. That
is the gap this closes, and it closes it the same way: locators and nodes are
attached to passages that already exist, leaving text, offsets and embeddings
untouched, because a locator is learned from the source and not from the words.

Verse spans come from `bible_layout`, which reads them back out of the stored
text under a coverage guard. A chapter whose parse leaves any text unaccounted
for is skipped and named, never written from a guess.

Usage (from the repository root):
    uv run python scripts/backfill_editions.py LHB --dry-run
    uv run python scripts/backfill_editions.py LHB
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bible_layout as B  # noqa: E402

PREFIX = {
    "LHB": "Lexham Hebrew Bible — ",
    "ESV": "English Standard Version — ",
}


def split_title(title: str, prefix: str) -> tuple[str, int]:
    """Book and chapter from a title. A one-chapter book names no chapter."""
    suffix = title.removeprefix(prefix)
    match = re.match(r"^(.*?) (\d+)$", suffix)
    return (match.group(1), int(match.group(2))) if match else (suffix, 1)


def build_locator(book: str, chapter: int, verses: list[int]) -> dict:
    first, last = verses[0], verses[-1]
    ref = f"{book} {chapter}:{first}" + (f"-{last}" if last != first else "")
    return {
        "ref": ref, "book": book, "chapter": chapter,
        "verse_start": first, "verse_end": last,
    }


def verses_in_span(text, regions, start, end) -> list[int]:
    """Verse numbers a passage's character span actually shows.

    Overlap has to be visible text: a chunk boundary can clip a neighbouring
    verse by a character or two of whitespace, and citing a verse the passage
    does not show would be worse than citing one fewer.
    """
    hit = []
    for region in regions:
        if region.kind != "verse":
            continue
        lo, hi = max(region.start, start), min(region.end, end)
        if lo < hi and text[lo:hi].strip():
            hit.append(region.number)
    return sorted(hit)


def build_drafts(book: str, chapter: int, edition: str, text: str, regions):
    """A chapter root, then one node per region in reading order."""
    from research_engine.domain.nodes import ROOT_PATH, DocumentNodeDraft

    drafts = [
        DocumentNodeDraft(
            path=ROOT_PATH, parent_path=None, depth=0, position=0,
            node_type="document", title=f"{book} {chapter}",
            char_start=0, char_end=len(text),
            metadata={"book": book, "chapter": chapter, "edition_key": edition},
        )
    ]
    position = 0
    for region in regions:
        if region.kind == "bracket":
            continue
        if region.kind == "verse":
            title = f"{book} {chapter}:{region.number}"
            meta = {"book": book, "chapter": chapter, "verse": region.number, "ref": title}
        elif region.kind == "foreign_verse":
            title = f"{book} {chapter - 1}:{region.number}"
            meta = {"book": book, "chapter": chapter - 1, "verse": region.number, "ref": title}
        elif region.kind == "superscription":
            title = f"{book} {chapter} superscription"
            meta = {"book": book, "chapter": chapter}
        else:
            title = region.title
            meta = {"book": book, "chapter": chapter}
        kind = "verse" if region.kind == "foreign_verse" else region.kind
        drafts.append(
            DocumentNodeDraft(
                path=f"{ROOT_PATH}.n{position}", parent_path=ROOT_PATH, depth=1,
                position=position, node_type=kind, title=title,
                char_start=region.start, char_end=region.end,
                metadata=meta | {"edition_key": edition},
            )
        )
        position += 1
    return drafts


async def resolve_regions(container, edition, doc_id, title, text, book, chapter):
    """Verse regions for one chapter, or (None, reason) if they cannot be trusted."""
    if edition == "LHB":
        regions, gap_re = B.parse_lhb(text, chapter), B.LHB_GAP
    else:
        regions, gap_re = B.parse_esv(text, chapter), B.ESV_GAP

    if regions is not None and not B.coverage_gaps(text, regions, gap_re):
        return regions, None

    # An unversified chapter carries nothing to number its verses by, so counting
    # blocks is only safe where an independent record of the numbering already
    # exists to check it against. Leviticus 25 has one: it was ingested for the
    # dossier with a per-verse locator on every passage.
    fallback = B.parse_unmarked(text)
    if B.coverage_gaps(text, fallback, B.LHB_GAP):
        return None, "no parse covers the text"

    passages = await container.passages.get_by_document(doc_id)
    recorded = {
        p.id: int(m.group(1))
        for p in passages
        if (loc := (p.locator or {})).get("ref")
        and (m := re.search(r":(\d+)$", loc["ref"]))
    }
    if len(recorded) != len(passages):
        return None, "unversified and no recorded numbering to check against"
    for passage in passages:
        derived = verses_in_span(text, fallback, passage.char_start, passage.char_end)
        if derived != [recorded[passage.id]]:
            return None, f"block numbering disagrees with recorded locators ({derived})"
    return fallback, None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("edition", choices=sorted(PREFIX))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import sqlalchemy as sa

    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.domain.nodes import deepest_containing

    container = await build_container(load_settings())
    edition = args.edition
    try:
        sql = sa.text(
            "SELECT d.id, d.title, t.text FROM core.documents d "
            "JOIN core.document_texts t ON t.document_id = d.id "
            "WHERE d.metadata->>'edition_key' = :k ORDER BY d.title"
        )
        async with container.engine.connect() as conn:
            docs = (await conn.execute(sql, {"k": edition})).fetchall()
        print(f"{len(docs)} {edition} documents")

        locators: list[tuple] = []
        stats = dict(docs=0, verses=0, headings=0, nodes=0, written=0,
                     skipped=0, replaced=0, attached=0, single=0, widest=0)
        refused: list[tuple[str, str]] = []

        for doc_id, title, text in docs:
            book, chapter = split_title(title, PREFIX[edition])
            regions, why = await resolve_regions(
                container, edition, doc_id, title, text, book, chapter
            )
            if regions is None:
                refused.append((title, why))
                continue

            stats["docs"] += 1
            stats["verses"] += sum(r.kind == "verse" for r in regions)
            stats["headings"] += sum(r.kind == "heading" for r in regions)

            passages = await container.passages.get_by_document(doc_id)
            for passage in passages:
                verses = verses_in_span(text, regions, passage.char_start, passage.char_end)
                if not verses:
                    continue
                locators.append((passage.id, build_locator(book, chapter, verses)))
                stats["single"] += len(verses) == 1
                stats["widest"] = max(stats["widest"], len(verses))

            drafts = build_drafts(book, chapter, edition, text, regions)
            existing = await container.document_nodes.get_tree(doc_id)
            unchanged = len(existing) == len(drafts) and all(
                n.path == d.path and n.char_start == d.char_start
                and n.char_end == d.char_end and n.title == d.title
                for n, d in zip(
                    sorted(existing, key=lambda n: (n.depth, n.position)),
                    drafts,
                    strict=True,
                )
            )

            if args.dry_run:
                stats["skipped" if unchanged else "written"] += 1
                stats["nodes"] += 0 if unchanged else len(drafts)
                continue

            if unchanged:
                stored = existing
                stats["skipped"] += 1
            else:
                async with container.transaction_factory() as tx:
                    if existing:
                        await container.document_nodes.delete_for_document(tx, doc_id)
                        stats["replaced"] += 1
                    stored = await container.document_nodes.insert_many(tx, doc_id, drafts)
                stats["written"] += 1
                stats["nodes"] += len(drafts)

            # Nodes written after ingest leave `passages.node_id` NULL, which
            # `locate_passage` reads as "this document has no structure" — a
            # confident lie once the tree is there.
            attachments = [
                (p.id, node.id if (node := deepest_containing(
                    stored, p.char_start, p.char_end)) else None)
                for p in passages
            ]
            pending = [
                (pid, nid) for pid, nid in attachments
                if next(p.node_id for p in passages if p.id == pid) != nid
            ]
            if pending:
                await container.passages.set_node_ids(pending)
                stats["attached"] += len(pending)

            done = stats["written"] + stats["skipped"]
            if done % 250 == 0:
                print(f"  {done}/{len(docs)} …")

        print(f"\nparsed {stats['docs']}/{len(docs)} chapters — "
              f"{stats['verses']} verses, {stats['headings']} headings")
        print(f"locators prepared: {len(locators)} "
              f"({stats['single']} single-verse, widest spans {stats['widest']})")
        if refused:
            print(f"REFUSED {len(refused)}:")
            for t, why in refused[:8]:
                print(f"  {t.split('— ')[-1]}: {why}")

        if args.dry_run:
            for _, loc in locators[:4]:
                print(f"  [dry] {loc}")
            print(f"  [dry] would write {stats['nodes']} nodes over {stats['written']} chapters")
            return 0

        written = await container.passages.set_locators(locators)
        print(f"set_locators reported {written} (asyncpg reports -1 for executemany)")
        print(f"nodes: {stats}")
        return 1 if refused else 0
    finally:
        await container.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
