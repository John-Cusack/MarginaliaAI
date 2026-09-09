"""Give the WLC chapters a verse-level structure tree.

`find_by_span` — "the deepest node whose span encloses this" — has existed in
`PGDocumentNodeRepo` with a dedicated index behind it and no caller anywhere in
the codebase. Wiring it into the quote verifier only helps if there is something
to find, and no Bible chapter in this corpus has ever had a node. So: one
`document` node per chapter, one `verse` node beneath it per verse, spans taken
from the same renderer that produced the stored text.

This is what lets a quotation report the verse it is in rather than the range of
the 500-token chunk it was retrieved from. A quotation running across a verse
boundary is enclosed by no verse and resolves to the chapter, which is correct:
naming either verse would be wrong.

Usage (from the repository root):
    uv run python scripts/backfill_nodes.py --dry-run
    uv run python scripts/backfill_nodes.py
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


def build_drafts(book: str, chapter: int, text: str, spans) -> list:
    """A chapter root plus one node per verse, parents before children."""
    from research_engine.domain.nodes import ROOT_PATH, DocumentNodeDraft

    drafts = [
        DocumentNodeDraft(
            path=ROOT_PATH,
            parent_path=None,
            depth=0,
            position=0,
            node_type="document",
            title=f"{book} {chapter}",
            char_start=0,
            char_end=len(text),
            metadata={"book": book, "chapter": chapter, "edition_key": EDITION_KEY},
        )
    ]
    for position, (verse, start, end) in enumerate(spans):
        drafts.append(
            DocumentNodeDraft(
                path=f"{ROOT_PATH}.n{position}",
                parent_path=ROOT_PATH,
                depth=1,
                position=position,
                node_type="verse",
                title=f"{book} {chapter}:{verse}",
                char_start=start,
                char_end=end,
                metadata={
                    "book": book,
                    "chapter": chapter,
                    "verse": verse,
                    "ref": f"{book} {chapter}:{verse}",
                },
            )
        )
    return drafts


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wlc-dir", type=Path, default=Path("/tmp/morphhb/wlc"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import sqlalchemy as sa

    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.domain.nodes import deepest_containing

    osis_to_code = {v: k for k, v in LHB_TO_OSIS.items()}
    chapters = {
        f"{osis_to_code[c.book]}.{c.number}": c for c in W.load_all(args.wlc_dir)
    }

    container = await build_container(load_settings())
    try:
        sql = sa.text(
            "SELECT d.id, d.title, d.metadata->>'article' AS article, md5(t.text) AS digest "
            "FROM core.documents d JOIN core.document_texts t ON t.document_id = d.id "
            "WHERE d.metadata->>'edition_key' = :key ORDER BY d.title"
        )
        async with container.engine.connect() as conn:
            docs = (await conn.execute(sql, {"key": EDITION_KEY})).fetchall()
        print(f"{len(docs)} {EDITION_KEY} documents")

        stats = {"written": 0, "skipped": 0, "nodes": 0, "replaced": 0, "attached": 0}
        drift: list[str] = []

        for doc_id, title, article, digest in docs:
            chapter_obj = chapters[article.split(".", 1)[1]]
            text, spans = W.render_chapter_with_spans(chapter_obj)
            text = unicodedata.normalize("NFC", text)
            if hashlib.md5(text.encode()).hexdigest() != digest:
                drift.append(title)
                continue

            suffix = title.removeprefix(f"{TITLE_PREFIX} — ")
            match = re.match(r"^(.*) (\d+)$", suffix)
            assert match, f"unparseable title: {title}"
            book, chapter_no = match.group(1), int(match.group(2))
            drafts = build_drafts(book, chapter_no, text, spans)

            existing = await container.document_nodes.get_tree(doc_id)
            # Already correct? Leave the tree alone. Node ids are referenced by
            # `passages.node_id`, so rewriting an identical tree would churn
            # foreign keys for nothing — but still re-check the attachments
            # below, which is what makes a re-run repair a half-finished one.
            unchanged = len(existing) == len(drafts) and all(
                n.path == d.path and n.char_start == d.char_start
                and n.char_end == d.char_end and n.title == d.title
                for n, d in zip(sorted(existing, key=lambda n: (n.depth, n.position)),
                                drafts, strict=True)
            )

            if args.dry_run:
                stats["skipped" if unchanged else "written"] += 1
                if not unchanged:
                    stats["nodes"] += len(drafts)
                if stats["written"] <= 3 and not unchanged:
                    print(f"  [dry] {title}: {len(drafts)} nodes "
                          f"(root + {len(spans)} verses), e.g. {drafts[1].title!r} "
                          f"[{drafts[1].char_start}:{drafts[1].char_end}]")
                continue

            if unchanged:
                stored = existing
                stats["skipped"] += 1
            else:
                async with container.transaction_factory() as tx:
                    if existing:
                        await container.document_nodes.delete_for_document(tx, doc_id)
                        stats["replaced"] += 1
                    stored = await container.document_nodes.insert_many(
                        tx, doc_id, drafts
                    )
                stats["written"] += 1
                stats["nodes"] += len(drafts)

            # Nodes written after ingest leave `passages.node_id` NULL, and
            # `locate_passage` reads that NULL as "no structure recorded" —
            # which would now be false. A multi-verse chunk is enclosed by no
            # single verse and resolves to the chapter, which is the honest
            # answer for a chunk that spans six of them.
            passages = await container.passages.get_by_document(doc_id)
            attachments = [
                (p.id, node.id if (node := deepest_containing(
                    stored, p.char_start, p.char_end)) else None)
                for p in passages
            ]
            pending = [(pid, nid) for pid, nid in attachments
                       if next(p.node_id for p in passages if p.id == pid) != nid]
            if pending:
                await container.passages.set_node_ids(pending)
                stats["attached"] += len(pending)
            done = stats["written"] + stats["skipped"]
            if done % 200 == 0:
                print(f"  {done}/{len(docs)} …")

        print(f"\ndone: {stats}")
        if drift:
            print(f"SKIPPED for text drift: {len(drift)} — {drift[:5]}")
        return 1 if drift else 0
    finally:
        await container.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
