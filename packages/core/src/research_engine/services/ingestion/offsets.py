"""Give a passage back the span it was written without.

3,320 passages in this corpus carry no `char_start` at all. They were written by
`prose_window` 1.0, which recorded `byte_start: 0` for everything, and they sit
in three documents whose canonical text was recovered long after the fact. With
no span they cannot be quote-verified, cannot be cited to a location, and cannot
be attached to a structure node — which is why the Sears edition can hold 681
dated letters and not one of its 882 passages points at any of them.

`reindex chunks` would fix it and re-embeds the corpus to do so. That is the
wrong price for this: the passages' *words* are correct, only their addresses
are missing. `prose_window` 1.0 rebuilt each chunk as ``" ".join(sentences)``,
so the stored text is the canonical text with its whitespace runs collapsed —
recoverable by matching, with no model involved.

The text is rewritten to the canonical slice, restoring the whitespace that was
flattened, so the engine's central invariant holds exactly::

    passage.text == canonical_text[passage.char_start:passage.char_end]

Only whitespace changes, and only ever back toward the source. A passage whose
recovered slice does not collapse to what was stored is left alone and reported.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog

from research_engine.domain.nodes import deepest_containing
from research_engine.services.text.anchoring import CanonicalIndex, collapse_whitespace

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger()


@dataclass
class OffsetReport:
    documents_examined: int = 0
    passages_without_a_span: int = 0
    recovered: int = 0
    text_restored: int = 0
    attached_to_a_node: int = 0
    unmatched: list[str] = field(default_factory=list)
    dry_run: bool = False


class OffsetRecoveryService:
    """Re-anchors span-less passages by matching their text. Embeds nothing."""

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

    async def recover(
        self, document_ids: list[UUID] | None = None, *, dry_run: bool = False
    ) -> OffsetReport:
        report = OffsetReport(dry_run=dry_run)
        for document_id in await self._documents_with_gaps(document_ids):
            report.documents_examined += 1
            await self._recover_one(document_id, report, dry_run=dry_run)
        return report

    async def _documents_with_gaps(
        self, document_ids: list[UUID] | None
    ) -> list[UUID]:
        stmt = (
            "SELECT DISTINCT p.document_id FROM core.passages p "
            "JOIN core.document_texts t ON t.document_id = p.document_id "
            "WHERE p.char_start IS NULL"
        )
        params: dict[str, Any] = {}
        if document_ids:
            stmt += " AND p.document_id = ANY(:ids)"
            params["ids"] = list(document_ids)
        async with self._engine.connect() as conn:
            return [row[0] for row in (await conn.execute(sa.text(stmt), params)).all()]

    async def _recover_one(
        self, document_id: UUID, report: OffsetReport, *, dry_run: bool
    ) -> None:
        stored_text = await self._texts.get(document_id)
        if stored_text is None or not stored_text.text:
            return
        canonical = stored_text.text
        index = CanonicalIndex(canonical)

        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT id, text FROM core.passages "
                        "WHERE document_id = :d AND char_start IS NULL "
                        "ORDER BY position"
                    ),
                    {"d": document_id},
                )
            ).all()
        report.passages_without_a_span += len(rows)
        if not rows:
            return

        nodes = await self._nodes.get_tree(document_id) if self._nodes else []
        updates: list[dict[str, Any]] = []
        # Passages run in document order, so each search resumes where the last
        # match ended. Without the hint, a paragraph repeated verbatim — a
        # running header, a stock telegram closing — anchors every occurrence to
        # the first one.
        cursor = 0
        for passage_id, text in rows:
            span = index.find(text, cursor)
            if span is None:
                report.unmatched.append(str(passage_id))
                continue
            slice_ = canonical[span.start : span.end]
            if collapse_whitespace(slice_).strip() != collapse_whitespace(text).strip():
                # Matched somewhere, but not to the same words. Refuse: a wrong
                # span is a citation that points at the wrong sentence.
                report.unmatched.append(str(passage_id))
                continue
            cursor = span.end
            node = deepest_containing(nodes, span.start, span.end) if nodes else None
            updates.append({
                "pid": passage_id,
                "s": span.start,
                "e": span.end,
                "t": slice_,
                "h": hashlib.sha256(slice_.encode()).digest(),
                "n": node.id if node else None,
            })
            if slice_ != text:
                report.text_restored += 1
            if node is not None:
                report.attached_to_a_node += 1

        if not updates:
            return
        async with self._transaction() as tx:
            await tx.conn.execute(
                sa.text(
                    "UPDATE core.passages SET char_start = :s, char_end = :e, "
                    "text = :t, content_hash = :h, node_id = :n WHERE id = :pid"
                ),
                updates,
            )
            if dry_run:
                await tx.conn.rollback()
        report.recovered += len(updates)
        logger.info(
            "offsets_recovered",
            document_id=str(document_id),
            recovered=len(updates),
            unmatched=len(rows) - len(updates),
        )
