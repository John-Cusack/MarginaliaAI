"""Stage 1: embedding shortlist over horn-eligible claims.

Stage 0 decides *which* claims may form a horn; this stage decides which
pairs are worth scoring. Every eligible claim's ``statement`` text — and
only that text, never evidence or attributes — is embedded once under one
(model, model_version), stored for resumability, and then used to query
the top-k nearest eligible neighbours. The result is a deduped set of
ordered pairs; the NLI stage decides what any pair means.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from research_engine.domain.machine_claims import ClaimEmbedding

if TYPE_CHECKING:
    from collections.abc import Collection
    from uuid import UUID

    from research_engine.domain.machine_claims import MachineClaim
    from research_engine.ports.embedding import EmbeddingPort


class ClaimShortlister:
    """Embeds eligible claim statements, then top-k pairs per claim."""

    def __init__(
        self,
        embedder: EmbeddingPort,
        claim_embeddings: Any,
        transaction_factory: Any,
    ) -> None:
        self._embedder = embedder
        self._embeddings = claim_embeddings
        self._transaction = transaction_factory

    async def shortlist(
        self,
        claims: list[MachineClaim],
        eligible_ids: Collection[UUID],
        k: int,
        model: str,
        model_version: str,
    ) -> list[tuple[UUID, UUID, float]]:
        """Return deduped ``(claim_a_id, claim_b_id, vec_score)`` pairs.

        Only claims whose id is in ``eligible_ids`` are embedded or
        searched; pairs are ordered so ``str(a) < str(b)`` and never
        contain a claim paired with itself.
        """
        eligible = set(eligible_ids)
        targets = [claim for claim in claims if claim.id in eligible]
        vectors = await self._ensure_embeddings(targets, model, model_version)
        if k <= 0:
            return []
        candidate_ids = [claim.id for claim in targets]
        pairs: dict[tuple[UUID, UUID], float] = {}
        for claim in targets:
            others = [cid for cid in candidate_ids if cid != claim.id]
            if not others:
                continue
            hits = await self._embeddings.vector_search(
                vectors[claim.id], model, model_version, others, k
            )
            for other_id, vec_score in hits[:k]:
                if other_id == claim.id or other_id not in eligible:
                    continue
                pair = _ordered(claim.id, other_id)
                pairs[pair] = max(pairs.get(pair, vec_score), vec_score)
        return [
            (a, b, pairs[(a, b)])
            for a, b in sorted(pairs, key=lambda p: (str(p[0]), str(p[1])))
        ]

    async def _ensure_embeddings(
        self,
        targets: list[MachineClaim],
        model: str,
        model_version: str,
    ) -> dict[UUID, list[float]]:
        """Return one stored vector per target, embedding the missing."""
        vectors: dict[UUID, list[float]] = {}
        missing: list[MachineClaim] = []
        for claim in targets:
            stored = await self._embeddings.get(claim.id, model, model_version)
            if stored is None:
                missing.append(claim)
            else:
                vectors[claim.id] = stored.embedding
        if missing:
            fresh = await self._embedder.embed_batch(
                [claim.statement for claim in missing]
            )
            rows = [
                ClaimEmbedding(
                    claim_id=claim.id,
                    model=model,
                    model_version=model_version,
                    embedding=vec,
                )
                for claim, vec in zip(missing, fresh, strict=True)
            ]
            async with self._transaction() as tx:
                await self._embeddings.store(tx, rows)
            for claim, vec in zip(missing, fresh, strict=True):
                vectors[claim.id] = vec
        return vectors


def _ordered(first: UUID, second: UUID) -> tuple[UUID, UUID]:
    """Order two claim ids so the smaller string sorts first."""
    if str(first) < str(second):
        return (first, second)
    return (second, first)
