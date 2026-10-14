"""Claim embeddings and NLI pair scores for the contradiction-finder funnel."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from research_engine.adapters.storage.postgres.schema import (
    claim_embeddings,
    pair_scores,
)
from research_engine.domain.machine_claims import (
    ClaimEmbedding,
    PairScore,
    PairScoreDraft,
)


def ordered_pair(a: UUID, b: UUID) -> tuple[UUID, UUID]:
    """Order a claim pair so the same pair never lands under two orders."""
    return (a, b) if str(a) < str(b) else (b, a)


class PGClaimEmbeddingRepo:
    """One vector per (claim, model, model_version), over statement text."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def store(self, tx: Transaction, rows: list[ClaimEmbedding]) -> None:
        """Store claim vectors, overwriting the same (claim, model, version).

        Re-embedding the same statement under the same model version is the
        normal retry path, so a repeat is a rewrite here, not an integrity
        error for the caller to decode.
        """
        if not rows:
            return
        stmt = pg_insert(claim_embeddings).values(
            [
                {
                    "claim_id": row.claim_id,
                    "model": row.model,
                    "model_version": row.model_version,
                    "embedding": row.embedding,
                }
                for row in rows
            ]
        )
        stmt = stmt.on_conflict_do_update(
            constraint="claim_embeddings_pkey",
            set_={"embedding": stmt.excluded.embedding},
        )
        await tx.conn.execute(stmt)

    async def get(self, claim_id: UUID, model: str, model_version: str) -> ClaimEmbedding | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    claim_embeddings.select().where(
                        sa.and_(
                            claim_embeddings.c.claim_id == claim_id,
                            claim_embeddings.c.model == model,
                            claim_embeddings.c.model_version == model_version,
                        )
                    )
                )
            ).first()
        if row is None:
            return None
        mapping = dict(row._mapping)
        return ClaimEmbedding(
            claim_id=mapping["claim_id"],
            model=mapping["model"],
            model_version=mapping["model_version"],
            embedding=list(mapping["embedding"]),
        )

    async def vector_search(
        self,
        query_vec: list[float],
        model: str,
        model_version: str,
        candidate_ids: list[UUID] | None,
        k: int,
    ) -> list[tuple[UUID, float]]:
        """Top-k claims by cosine over the eligible candidates, if any."""
        async with self._engine.connect() as conn:
            embedding_str = f"[{','.join(str(x) for x in query_vec)}]"
            sql = sa.text("""
                SELECT ce.claim_id,
                       1 - (ce.embedding <=> CAST(:qv AS vector)) AS vec_score
                FROM dilemma.claim_embeddings ce
                WHERE ce.model = :model AND ce.model_version = :mv
                  AND (:no_filter OR ce.claim_id = ANY(:candidate_ids))
                ORDER BY ce.embedding <=> CAST(:qv AS vector)
                LIMIT :k
            """)
            result = await conn.execute(
                sql,
                {
                    "qv": embedding_str,
                    "model": model,
                    "mv": model_version,
                    "no_filter": candidate_ids is None,
                    "candidate_ids": candidate_ids or [],
                    "k": k,
                },
            )
            return [(row.claim_id, row.vec_score) for row in result]


class PGPairScoreRepo:
    """NLI verdicts on ordered claim pairs, one row per (run, a, b)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, run_id: UUID, rows: list[PairScoreDraft]) -> list[PairScore]:
        """Store verdicts, silently keeping the first write of a repeat.

        Re-scoring the same pair under the same run is the normal retry path,
        so a duplicate is a no-op here (``ON CONFLICT DO NOTHING``), and only
        the newly written rows come back.
        """
        if not rows:
            return []
        async with self._engine.begin() as conn:
            result = await conn.execute(
                pg_insert(pair_scores)
                .values(
                    [
                        {
                            "run_id": run_id,
                            "claim_a_id": lo,
                            "claim_b_id": hi,
                            "relation": draft.relation.value,
                            "confidence": draft.confidence,
                            "rationale": draft.rationale,
                            "llm_call_id": draft.llm_call_id,
                            "model": draft.model,
                            "prompt_hash": draft.prompt_hash,
                        }
                        for draft in rows
                        for lo, hi in [ordered_pair(draft.claim_a_id, draft.claim_b_id)]
                    ]
                )
                .on_conflict_do_nothing(
                    constraint="pair_scores_pkey",
                )
                .returning(pair_scores)
            )
            return [PairScore.model_validate(row._mapping) for row in result.all()]

    async def for_run(self, run_id: UUID) -> list[PairScore]:
        """Every verdict a run stored, oldest first."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    pair_scores.select()
                    .where(pair_scores.c.run_id == run_id)
                    .order_by(pair_scores.c.created_at)
                )
            ).all()
        return [PairScore.model_validate(row._mapping) for row in rows]


#: Short names matching the funnel contract; the PG names match repo convention.
ClaimEmbeddingRepo = PGClaimEmbeddingRepo
PairScoreRepo = PGPairScoreRepo
