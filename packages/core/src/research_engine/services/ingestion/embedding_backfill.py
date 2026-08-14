"""Find and fill gaps in embedding coverage.

A passage with no embedding under the active model is invisible to semantic
search while remaining findable by keyword — so the gap presents as a ranking
quirk rather than as missing data, and can sit unnoticed indefinitely. That is
exactly how 2,095 passages of real library books came to have only a test
embedder's 8-dimensional vectors.

This is also the recovery path for an interrupted ingest, which is why it is a
permanent tool rather than a migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.schema import (
    documents,
    passage_embeddings,
    passages,
)
from research_engine.services.ingestion.embed_batches import embed_and_store

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger()




@dataclass
class CoverageReport:
    """Embedding coverage for the active model."""

    model: str
    model_version: str
    dim: int
    total_passages: int = 0
    embedded: int = 0
    missing: int = 0
    wrong_dimension: int = 0
    foreign_models: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.missing == 0 and self.wrong_dimension == 0

    @property
    def coverage(self) -> float:
        return self.embedded / self.total_passages if self.total_passages else 1.0


@dataclass
class BackfillReport:
    dry_run: bool = False
    candidates: int = 0
    embedded: int = 0
    failed_batches: int = 0
    #: Passages that failed even alone — a genuinely unembeddable passage,
    #: not a memory pressure artefact.
    failed_passages: list[UUID] = field(default_factory=list)
    #: Times a batch had to be split. A high count means embedding_batch_size
    #: is too large for this corpus and hardware.
    halvings: int = 0


class EmbeddingBackfillService:
    """Reports and repairs embedding coverage."""

    def __init__(
        self,
        engine: AsyncEngine,
        passage_repo: Any,
        embedding: Any,
        batch_size: int = 32,
    ) -> None:
        self._engine = engine
        self._passages = passage_repo
        self._embedding = embedding
        self._batch_size = batch_size

    async def coverage(self) -> CoverageReport:
        model = self._embedding.model_name
        version = self._embedding.model_version
        dim = self._embedding.dim

        active = sa.and_(
            passage_embeddings.c.model == model,
            passage_embeddings.c.model_version == version,
        )
        async with self._engine.connect() as conn:
            total = (
                await conn.execute(sa.select(sa.func.count()).select_from(passages))
            ).scalar_one()
            embedded = (
                await conn.execute(
                    sa.select(sa.func.count(sa.distinct(passage_embeddings.c.passage_id)))
                    .where(active)
                )
            ).scalar_one()
            wrong_dim = (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(passage_embeddings)
                    .where(sa.and_(active, passage_embeddings.c.dim != dim))
                )
            ).scalar_one()
            foreign = (
                await conn.execute(
                    sa.select(passage_embeddings.c.model, sa.func.count())
                    .where(passage_embeddings.c.model != model)
                    .group_by(passage_embeddings.c.model)
                )
            ).all()

        return CoverageReport(
            model=model,
            model_version=version,
            dim=dim,
            total_passages=total,
            embedded=embedded,
            missing=total - embedded,
            wrong_dimension=wrong_dim,
            foreign_models={row[0]: row[1] for row in foreign},
        )

    async def missing_passage_ids(self, limit: int | None = None) -> list[UUID]:
        """Passages with no embedding under the active model, oldest first."""
        active = sa.and_(
            passage_embeddings.c.passage_id == passages.c.id,
            passage_embeddings.c.model == self._embedding.model_name,
            passage_embeddings.c.model_version == self._embedding.model_version,
        )
        stmt = (
            sa.select(passages.c.id)
            .where(~sa.exists(sa.select(sa.literal(1)).where(active)))
            .order_by(passages.c.document_id, passages.c.position)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._engine.connect() as conn:
            return [row[0] for row in await conn.execute(stmt)]

    async def backfill(
        self, *, dry_run: bool = False, limit: int | None = None
    ) -> BackfillReport:
        """Embed every passage missing an embedding under the active model.

        Batched and resumable: each batch commits on its own, so an interrupted
        run leaves committed work in place and the next run picks up the
        remainder rather than starting over.
        """
        report = BackfillReport(dry_run=dry_run)
        candidates = await self.missing_passage_ids(limit)
        report.candidates = len(candidates)
        if dry_run or not candidates:
            return report

        for start in range(0, len(candidates), self._batch_size):
            batch_ids = candidates[start : start + self._batch_size]
            async with self._engine.connect() as conn:
                rows = (
                    await conn.execute(
                        sa.select(passages.c.id, passages.c.text).where(
                            passages.c.id.in_(batch_ids)
                        )
                    )
                ).all()
            if rows:
                await self._embed_and_store(
                    [r.id for r in rows], [r.text for r in rows], report
                )

        return report

    async def _embed_and_store(
        self, ids: list[UUID], texts: list[str], report: BackfillReport
    ) -> None:
        """Embed a batch and commit it, halving on failure.

        Each batch commits on its own, so an interrupted run leaves committed
        work in place and the next run picks up the remainder.
        """

        async def store(batch_ids: list[UUID], vectors: list[list[float]]) -> None:
            async with transaction(self._engine) as tx:
                await self._passages.store_embeddings(
                    tx,
                    batch_ids,
                    vectors,
                    self._embedding.model_name,
                    self._embedding.model_version,
                    self._embedding.dim,
                )

        outcome = await embed_and_store(self._embedding, ids, texts, store)
        report.embedded += outcome.embedded
        report.failed_batches += outcome.failed_batches
        report.halvings += outcome.halvings
        report.failed_passages.extend(outcome.failed_passages)

    async def purge_model(self, model: str, *, dry_run: bool = False) -> int:
        """Delete every embedding written by *model*.

        For removing vectors a test harness or an abandoned model left behind.
        Refuses to touch the active model: that would be a corpus-wide delete
        dressed as a cleanup.
        """
        if model == self._embedding.model_name:
            raise ValueError(
                f"Refusing to purge the active embedding model {model!r}. "
                "Point the engine at a different model first if that is really "
                "what you mean."
            )
        async with self._engine.connect() as conn:
            count = (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(passage_embeddings)
                    .where(passage_embeddings.c.model == model)
                )
            ).scalar_one()
        if dry_run or not count:
            return count

        async with self._engine.begin() as conn:
            await conn.execute(
                passage_embeddings.delete().where(passage_embeddings.c.model == model)
            )
        logger.info("embeddings_purged", model=model, rows=count)
        return count

    async def coverage_by_document_type(self) -> list[tuple[str, int, int]]:
        """(document_type, passages, embedded) — where the gaps actually are."""
        active = sa.and_(
            passage_embeddings.c.passage_id == passages.c.id,
            passage_embeddings.c.model == self._embedding.model_name,
            passage_embeddings.c.model_version == self._embedding.model_version,
        )
        stmt = (
            sa.select(
                documents.c.document_type,
                sa.func.count(passages.c.id),
                sa.func.count().filter(sa.exists(sa.select(sa.literal(1)).where(active))),
            )
            .select_from(passages.join(documents, documents.c.id == passages.c.document_id))
            .group_by(documents.c.document_type)
            .order_by(documents.c.document_type)
        )
        async with self._engine.connect() as conn:
            return [(r[0], r[1], r[2]) for r in await conn.execute(stmt)]
