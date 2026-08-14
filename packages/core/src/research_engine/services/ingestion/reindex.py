"""Non-destructive re-chunking.

Deleting passages to re-chunk is a data-loss operation wearing the costume of a
migration: ``mentions``, ``extractions`` and ``extraction_records`` are
``ON DELETE CASCADE`` on ``passages.id``, and ``events.source_passage_id`` /
``edges.source_passage_id`` are ``SET NULL``. A naive re-chunk destroys every
extraction and mention in the corpus and silently nulls event and edge
provenance.

This module re-chunks by *re-anchoring* instead:

1. Load the document's canonical text.
2. Insert new passages under the new ``chunker_version``. The existing
   ``(document_id, position, chunker, chunker_version)`` unique constraint lets
   old and new rows coexist, which is what makes step 3 safe to run before
   step 4.
3. Repoint every dependent row from its old passage to the new passage that best
   covers the same span.
4. Delete the old passages. Cascade now has nothing left to destroy.

Everything for one document happens in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.schema import (
    documents,
    edges,
    events,
    extraction_records,
    extractions,
    mentions,
    passages,
)
from research_engine.services.ingestion.embed_batches import BatchOutcome, embed_and_store
from research_engine.services.ingestion.pipeline import run_chunking
from research_engine.services.search.langconfig import pg_config
from research_engine.services.text.anchoring import CanonicalIndex, Span, best_overlap

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger()

#: Fail the run above this fraction of unmatched passages. A silent 3% loss of
#: extractions is exactly the failure this phase exists to prevent.
DEFAULT_ORPHAN_THRESHOLD = 0.005

#: (table, column) pairs that reference a passage and carry research content.
#: `passage_embeddings` and `passage_fts` are deliberately absent: they are
#: derived from passage text and are regenerated, so cascading them away with
#: the old rows is correct.
DEPENDENTS: tuple[tuple[sa.Table, str], ...] = (
    (mentions, "passage_id"),
    (extractions, "passage_id"),
    (extraction_records, "passage_id"),
    (events, "source_passage_id"),
    (edges, "source_passage_id"),
)


@dataclass
class Orphan:
    """An old passage whose text could not be located in the canonical text."""

    document_id: UUID
    passage_id: UUID
    text_preview: str
    dependents: dict[str, int]
    reason: str

    @property
    def dependent_total(self) -> int:
        return sum(self.dependents.values())


@dataclass
class ReindexReport:
    dry_run: bool = False
    documents_total: int = 0
    documents_reindexed: int = 0
    documents_up_to_date: int = 0
    documents_without_text: list[UUID] = field(default_factory=list)
    #: Structurally chunked documents, which cannot be re-chunked from canonical
    #: text alone — their section decomposition lives on the document, not here.
    documents_needing_reingest: list[UUID] = field(default_factory=list)
    documents_failed: dict[str, str] = field(default_factory=dict)
    passages_before: int = 0
    passages_after: int = 0
    repointed: dict[str, int] = field(default_factory=dict)
    orphans: list[Orphan] = field(default_factory=list)
    collisions: int = 0
    #: True when the preflight pass refused to let the real pass run.
    aborted: bool = False

    @property
    def orphan_rate(self) -> float:
        return len(self.orphans) / self.passages_before if self.passages_before else 0.0

    @property
    def orphaned_dependents(self) -> int:
        return sum(o.dependent_total for o in self.orphans)

    def exceeded(self, threshold: float) -> bool:
        return self.orphan_rate > threshold


class ReindexService:
    """Re-chunks documents onto current chunker versions without losing links."""

    def __init__(
        self,
        engine: AsyncEngine,
        passage_repo: Any,
        document_text_repo: Any,
        embedding: Any,
        orphan_threshold: float = DEFAULT_ORPHAN_THRESHOLD,
        embedding_batch_size: int = 32,
    ) -> None:
        self._engine = engine
        self._passages = passage_repo
        self._texts = document_text_repo
        #: Required, not optional. `passage_embeddings` and `passage_fts` cascade
        #: away with the old passages; a reindexer that cannot regenerate them
        #: would quietly drop the document out of both vector and keyword search.
        self._embedding = embedding
        self._orphan_threshold = orphan_threshold
        self._embedding_batch_size = embedding_batch_size

    async def reindex_chunks(
        self,
        document_ids: Sequence[UUID] | None = None,
        *,
        dry_run: bool = False,
    ) -> ReindexReport:
        """Re-chunk *document_ids* (or every stale document) and re-anchor links.

        A real run is preceded by a full preflight pass that does the same work
        and rolls back. Without it the orphan threshold would be meaningless:
        each document commits as it goes, so by the time the rate is known the
        passages are already deleted.
        """
        targets = list(document_ids) if document_ids is not None else await self._stale_documents()

        if not dry_run:
            preflight = await self._run_pass(targets, dry_run=True)
            if preflight.exceeded(self._orphan_threshold):
                preflight.aborted = True
                logger.error(
                    "reindex_aborted_orphan_rate",
                    orphan_rate=preflight.orphan_rate,
                    threshold=self._orphan_threshold,
                    dependents_at_risk=preflight.orphaned_dependents,
                )
                return preflight

        return await self._run_pass(targets, dry_run=dry_run)

    async def _run_pass(self, targets: list[UUID], *, dry_run: bool) -> ReindexReport:
        report = ReindexReport(dry_run=dry_run)
        report.documents_total = len(targets)

        for document_id in targets:
            try:
                await self._reindex_one(document_id, report, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
                logger.error("reindex_document_failed", document_id=str(document_id), error=str(exc))
                report.documents_failed[str(document_id)] = str(exc)

        return report

    async def _stale_documents(self) -> list[UUID]:
        """Documents holding passages from a chunker version we no longer emit."""
        from research_engine.services.ingestion.pipeline import current_chunker_versions

        current = current_chunker_versions()
        if not current:
            return []

        stale = sa.or_(
            *[
                sa.and_(passages.c.chunker == chunker, passages.c.chunker_version != version)
                for chunker, version in current.items()
            ]
        )
        stmt = (
            sa.select(passages.c.document_id)
            .where(stale)
            .distinct()
            .order_by(passages.c.document_id)
        )
        async with self._engine.connect() as conn:
            return [row[0] for row in await conn.execute(stmt)]

    async def _reindex_one(
        self, document_id: UUID, report: ReindexReport, *, dry_run: bool
    ) -> None:
        canonical_text = await self._texts.get_text(document_id)
        if canonical_text is None:
            # Reconstructing text from overlapping chunks is not reliable, and a
            # wrong substrate would produce confidently wrong offsets. Skip and
            # say so: these documents need re-ingesting.
            report.documents_without_text.append(document_id)
            return

        old_passages = await self._passages.get_by_document(document_id)
        if not old_passages:
            return

        chunker_id = old_passages[0].chunker
        if chunker_id == "structural":
            # Structural chunking is driven by the parser's section table, which
            # is stored on the document rather than reachable from here. Without
            # it `run_chunking` would silently fall back to prose windows and
            # quietly demote the document out of structural chunking — losing
            # its headings, and with them every pin-cite built on them. Refusing
            # and naming the document is the only honest option.
            report.documents_needing_reingest.append(document_id)
            return

        new_drafts = await run_chunking(canonical_text, chunker_id)
        if not new_drafts:
            report.documents_failed[str(document_id)] = "chunker produced no passages"
            return

        if all(p.chunker_version == new_drafts[0].chunker_version for p in old_passages):
            report.documents_up_to_date += 1
            return

        report.passages_before += len(old_passages)
        report.passages_after += len(new_drafts)

        # A dry run does the whole thing and rolls back, rather than simulating
        # it. That way constraint violations and mapping failures surface now,
        # on the run whose whole purpose is to surface them.
        try:
            async with transaction(self._engine) as tx:
                new_passages = await self._passages.insert_many(tx, document_id, new_drafts)
                new_spans = [
                    (p.id, Span(p.char_start, p.char_end))
                    for p in new_passages
                    if p.char_start is not None and p.char_end is not None
                ]

                index = CanonicalIndex(canonical_text)
                cursor = 0
                remapping: dict[UUID, UUID] = {}

                for old in old_passages:
                    span = index.find(old.text, cursor)
                    if span is None:
                        report.orphans.append(
                            await self._describe_orphan(
                                tx, document_id, old, "text not found in canonical text"
                            )
                        )
                        continue
                    cursor = span.start

                    target = best_overlap(span, new_spans)
                    if target is None:
                        report.orphans.append(
                            await self._describe_orphan(
                                tx, document_id, old, "no new passage covers the span"
                            )
                        )
                        continue
                    remapping[old.id] = target

                await self._repoint(tx, remapping, report)
                await tx.conn.execute(
                    passages.delete().where(passages.c.id.in_([p.id for p in old_passages]))
                )

                if dry_run:
                    # Embedding is skipped here and only here: a preflight over
                    # the whole corpus would embed every passage and throw the
                    # result away. The real pass below always does it.
                    report.documents_reindexed += 1
                    raise _Rollback

                await self._rebuild_search_artifacts(tx, document_id, new_passages)
                report.documents_reindexed += 1
        except _Rollback:
            pass

    async def _rebuild_search_artifacts(
        self, tx: Any, document_id: UUID, new_passages: list[Any]
    ) -> None:
        """Re-embed and re-index the replacement passages.

        Both tables cascade off ``passages.id``, so deleting the old rows takes
        the document's embeddings and FTS entries with them. Without this the
        re-chunk would succeed, keep every extraction and mention, and leave the
        document unfindable by any search — the worst kind of quiet failure,
        because nothing errors.

        Runs inside the caller's transaction, so a failure here rolls the whole
        document back rather than leaving it half-indexed.
        """
        if not new_passages:
            return

        ids = [p.id for p in new_passages]
        texts = [p.text for p in new_passages]

        async def store(batch_ids: list[UUID], vectors: list[list[float]]) -> None:
            await self._passages.store_embeddings(
                tx,
                batch_ids,
                vectors,
                self._embedding.model_name,
                self._embedding.model_version,
                self._embedding.dim,
            )

        outcome = BatchOutcome()
        for i in range(0, len(texts), self._embedding_batch_size):
            await embed_and_store(
                self._embedding,
                ids[i : i + self._embedding_batch_size],
                texts[i : i + self._embedding_batch_size],
                store,
                outcome,
            )

        if outcome.failed_passages:
            # Raising rolls the document back whole rather than committing it
            # half-embedded — the passages would be silently unsearchable.
            raise RuntimeError(
                f"{len(outcome.failed_passages)} passage(s) could not be embedded "
                f"even individually; document left unchanged"
            )

        language = await self._document_language(document_id)
        await self._passages.index_fts(tx, ids, texts, pg_config(language))

    async def _document_language(self, document_id: UUID) -> str | None:
        async with self._engine.connect() as conn:
            return (
                await conn.execute(
                    sa.select(documents.c.language).where(documents.c.id == document_id)
                )
            ).scalar_one_or_none()

    async def _repoint(
        self, tx: Any, remapping: dict[UUID, UUID], report: ReindexReport
    ) -> None:
        for table, column in DEPENDENTS:
            name = table.name
            for old_id, new_id in remapping.items():
                stmt = table.update().where(table.c[column] == old_id)

                if table is extractions:
                    # (passage_id, schema_id, extractor_version) is unique. Two
                    # old passages can map onto one new passage, so guard the
                    # move; anything blocked stays on the old row and is
                    # cascade-deleted with it, and is reported as a collision.
                    # One alias object, referenced three times — three calls to
                    # .alias() would produce three tables and a cartesian join.
                    existing = extractions.alias("existing")
                    conflict = sa.select(sa.literal(1)).where(
                        sa.and_(
                            existing.c.passage_id == new_id,
                            existing.c.schema_id == extractions.c.schema_id,
                            existing.c.extractor_version == extractions.c.extractor_version,
                        )
                    )
                    stmt = stmt.where(~sa.exists(conflict))

                result = await tx.conn.execute(stmt.values(**{column: new_id}))
                if result.rowcount:
                    report.repointed[name] = report.repointed.get(name, 0) + result.rowcount

                if table is extractions:
                    blocked = (
                        await tx.conn.execute(
                            sa.select(sa.func.count())
                            .select_from(extractions)
                            .where(extractions.c.passage_id == old_id)
                        )
                    ).scalar_one()
                    report.collisions += blocked

    async def _describe_orphan(
        self, tx: Any, document_id: UUID, old: Any, reason: str
    ) -> Orphan:
        counts: dict[str, int] = {}
        for table, column in DEPENDENTS:
            count = (
                await tx.conn.execute(
                    sa.select(sa.func.count())
                    .select_from(table)
                    .where(table.c[column] == old.id)
                )
            ).scalar_one()
            if count:
                counts[table.name] = count
        return Orphan(
            document_id=document_id,
            passage_id=old.id,
            text_preview=old.text[:120],
            dependents=counts,
            reason=reason,
        )


class _Rollback(Exception):
    """Abort a dry-run transaction so nothing is written."""
