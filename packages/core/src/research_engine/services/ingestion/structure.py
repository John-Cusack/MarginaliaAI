"""Rebuild a document's structure tree, and date its sections.

Two things brought this about.

`document_nodes` is written at ingest and, since a later change, by
`reindex chunks`. Neither reaches a document whose canonical text arrived
*after* its last re-chunk — which is exactly what happened to the two McClellan
volumes: 950 markdown headings between them and not one node, so
`get_document_outline` had nothing to say about either. Rebuilding structure
does not need the embedding server, and tying it to a command that does meant
the tree could not be repaired while the GPU host was off.

The second is dates. A section that opens with a date is a letter, a diary
entry, or a dated report, and the date is the single most useful thing to know
about it — it is what makes "yours of the 3d ult." resolvable, since a relative
date is relative to the letter it appears in. A bound volume of correspondence
is one document with one date, or none; its letters have hundreds. Recording
them on the nodes puts the date at the granularity it actually varies at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog

from research_engine.domain.nodes import (
    DocumentNodeDraft,
    build_node_tree,
    deepest_containing,
)
from research_engine.services.text.dates import dominant_century, scan_dates
from research_engine.services.text.sections import (
    sections_from_chapters,
    sections_from_markdown,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger()

#: How far into a section to look for its date. Measured over 728 letters: half
#: the datelines start within 51 characters and nine in ten within 105. Reading
#: further starts finding dates mentioned in the body, which are not the
#: section's own date and are worse than none.
DATELINE_WINDOW = 200


@dataclass
class StructureReport:
    documents_total: int = 0
    documents_rebuilt: int = 0
    documents_without_text: list[str] = field(default_factory=list)
    nodes_written: int = 0
    nodes_dated: int = 0
    passages_repointed: int = 0
    dry_run: bool = False
    failures: dict[str, str] = field(default_factory=dict)


class StructureService:
    """Rewrites `document_nodes` from canonical text. Touches no passage text."""

    def __init__(
        self,
        engine: AsyncEngine,
        document_texts: Any,
        document_nodes: Any,
        transaction_factory: Any,
    ) -> None:
        self._engine = engine
        self._texts = document_texts
        self._nodes = document_nodes
        self._transaction = transaction_factory

    async def rebuild(
        self,
        document_ids: list[UUID] | None = None,
        *,
        dry_run: bool = False,
        only_missing: bool = False,
    ) -> StructureReport:
        report = StructureReport(dry_run=dry_run)
        for document_id, title in await self._candidates(document_ids, only_missing):
            report.documents_total += 1
            try:
                await self._rebuild_one(document_id, title, report, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - one document must not stop the run
                logger.warning(
                    "structure_rebuild_failed", document_id=str(document_id), error=str(exc)
                )
                report.failures[str(document_id)] = str(exc)
        return report

    async def _candidates(
        self, document_ids: list[UUID] | None, only_missing: bool
    ) -> list[tuple[UUID, str | None]]:
        stmt = (
            "SELECT d.id, d.title FROM core.documents d "
            "JOIN core.document_texts t ON t.document_id = d.id"
        )
        clauses = []
        params: dict[str, Any] = {}
        if document_ids:
            clauses.append("d.id = ANY(:ids)")
            params["ids"] = list(document_ids)
        if only_missing:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM core.document_nodes n "
                "WHERE n.document_id = d.id)"
            )
        if clauses:
            stmt += " WHERE " + " AND ".join(clauses)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sa.text(stmt + " ORDER BY d.id"), params)).all()
        return [(row[0], row[1]) for row in rows]

    async def _rebuild_one(
        self,
        document_id: UUID,
        title: str | None,
        report: StructureReport,
        *,
        dry_run: bool,
    ) -> None:
        stored_text = await self._texts.get(document_id)
        if stored_text is None or not stored_text.text:
            report.documents_without_text.append(str(document_id))
            return
        # `text` is the authoritative substrate passage offsets address;
        # `normalized_text` is a lossy fold and would give offsets that address
        # nothing.
        text = stored_text.text

        drafts = node_tree_for(text, title=title)
        dated = sum(1 for d in drafts if d.metadata.get("date_start"))

        async with self._transaction() as tx:
            await self._nodes.delete_for_document(tx, document_id)
            stored = await self._nodes.insert_many(tx, document_id, drafts)
            repointed = await _repoint_passages(tx, document_id, stored)
            if dry_run:
                await tx.conn.rollback()

        report.documents_rebuilt += 1
        report.nodes_written += len(stored)
        report.nodes_dated += dated
        report.passages_repointed += repointed


def node_tree_for(text: str, *, title: str | None = None) -> list[DocumentNodeDraft]:
    """The one way a document's structure tree is built, for every caller.

    Two commands write `document_nodes`: this service and `reindex chunks`.
    They built the tree separately, and the second one used the undated
    sections — so re-chunking a volume of correspondence deleted its nodes and
    put them back without a single date, undoing the whole point of dating
    them. Both call this now, so a third caller cannot reintroduce the split.
    """
    return build_node_tree(dated_sections(text), text_length=len(text), title=title)


def dated_sections(text: str) -> list[dict[str, Any]]:
    """Sections, each carrying its own date where it states one.

    Markdown headings first, chapter lines only when there are none. The order
    matters and the fallback is not a second opinion: a document with `#`
    headings has an author's own structure, while chapter detection is inference
    over prose and should never override it.
    """
    sections = sections_from_markdown(text) or sections_from_chapters(text)
    if not sections:
        return sections
    century = dominant_century(text)
    for section in sections:
        opening = text[
            section["char_start"] : min(
                section["char_start"] + DATELINE_WINDOW, section["char_end"]
            )
        ]
        found = scan_dates(opening, century=century)
        if not found:
            continue
        _, _, date = found[0]
        # Straight onto the section, not nested under a "metadata" key:
        # `build_node_tree` folds every key it does not recognise into the
        # node's metadata, so nesting here would bury them a level down.
        section["date_start"] = date.start.isoformat()
        section["date_end"] = date.end.isoformat()
        section["date_precision"] = str(date.precision)
    return sections


async def _repoint_passages(tx: Any, document_id: UUID, nodes: list[Any]) -> int:
    """Point existing passages at the rebuilt tree, by span containment.

    Passage text and embeddings are untouched — only `node_id` moves — which is
    what lets structure be repaired without an embedding server.
    """
    if not nodes:
        return 0
    rows = (
        await tx.conn.execute(
            sa.text(
                "SELECT id, char_start, char_end FROM core.passages "
                "WHERE document_id = :d AND char_start IS NOT NULL"
            ),
            {"d": document_id},
        )
    ).all()
    updates = [
        {"pid": row[0], "nid": node.id}
        for row in rows
        if (node := deepest_containing(nodes, row[1], row[2])) is not None
    ]
    if not updates:
        return 0
    await tx.conn.execute(
        sa.text("UPDATE core.passages SET node_id = :nid WHERE id = :pid"), updates
    )
    return len(updates)
