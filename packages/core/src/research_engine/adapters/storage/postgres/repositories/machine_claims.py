"""Machine claims, their evidence spans, and the vouches they assert."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import (
    claim_evidence,
    machine_claims,
    vouch_edges,
)
from research_engine.domain.machine_claims import (
    ClaimEvidence,
    ClaimEvidenceDraft,
    MachineClaim,
    MachineClaimDraft,
    VouchEdge,
    VouchEdgeDraft,
)

if TYPE_CHECKING:

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGMachineClaimRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert_claim(
        self, tx: Transaction, draft: MachineClaimDraft
    ) -> MachineClaim | None:
        """Store one claim, or None when this record already materialized it.

        ``(extraction_record_id, statement)`` is unique, and re-materializing
        a run is the normal retry path, so a duplicate is an answer here, not
        an integrity error for the caller to decode.
        """
        stmt = (
            pg_insert(machine_claims)
            .values(
                id=uuid7(),
                run_id=draft.run_id,
                extraction_record_id=draft.extraction_record_id,
                document_id=draft.document_id,
                edition_id=draft.edition_id,
                statement=draft.statement,
                subject_label=draft.subject_label,
                subject_ref=draft.subject_ref,
                subject_entity_id=draft.subject_entity_id,
                predicate=draft.predicate,
                object_label=draft.object_label,
                polarity=draft.polarity.value,
                modality=draft.modality.value,
                speaker_label=draft.speaker_label,
                speaker_ref=draft.speaker_ref,
                speaker_entity_id=draft.speaker_entity_id,
                scope_quantifier=draft.scope_quantifier,
                temporal_scope=draft.temporal_scope,
                topic_keys=draft.topic_keys,
                confidence=draft.confidence,
                attributes=draft.attributes,
            )
            .on_conflict_do_nothing(constraint="claims_record_statement_uq")
            .returning(machine_claims)
        )
        row = (await tx.conn.execute(stmt)).first()
        return MachineClaim.model_validate(row._mapping) if row else None

    async def insert_evidence(
        self, tx: Transaction, drafts: list[ClaimEvidenceDraft]
    ) -> list[ClaimEvidence]:
        """Store a claim's evidence rows, in the order given."""
        if not drafts:
            return []
        stored = [
            ClaimEvidence(id=UUID(str(uuid7())), **draft.model_dump())
            for draft in drafts
        ]
        await tx.conn.execute(
            claim_evidence.insert(),
            [row.model_dump() for row in stored],
        )
        return stored

    async def insert_vouch(
        self, tx: Transaction, draft: VouchEdgeDraft
    ) -> VouchEdge:
        row = (
            await tx.conn.execute(
                vouch_edges.insert()
                .values(
                    id=uuid7(),
                    claim_id=draft.claim_id,
                    run_id=draft.run_id,
                    kind=draft.kind.value,
                    target_label=draft.target_label,
                    target_ref=draft.target_ref,
                    target_entity_id=draft.target_entity_id,
                    target_edition_id=draft.target_edition_id,
                    target_form=draft.target_form.value,
                    attributes=draft.attributes,
                )
                .returning(vouch_edges)
            )
        ).one()
        return VouchEdge.model_validate(row._mapping)

    async def for_run(self, run_id: UUID) -> list[MachineClaim]:
        """Every claim a run materialized, oldest first."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    machine_claims.select()
                    .where(machine_claims.c.run_id == run_id)
                    .order_by(machine_claims.c.created_at, machine_claims.c.id)
                )
            ).all()
        return [MachineClaim.model_validate(row._mapping) for row in rows]

    async def get(self, claim_id: UUID) -> MachineClaim | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    machine_claims.select().where(machine_claims.c.id == claim_id)
                )
            ).first()
        return MachineClaim.model_validate(row._mapping) if row else None

    async def evidence_for_claim(self, claim_id: UUID) -> list[ClaimEvidence]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    claim_evidence.select()
                    .where(claim_evidence.c.claim_id == claim_id)
                    .order_by(claim_evidence.c.ordinal)
                )
            ).all()
        return [ClaimEvidence.model_validate(row._mapping) for row in rows]

    async def evidence_for_claims(
        self, claim_ids: list[UUID]
    ) -> dict[UUID, list[ClaimEvidence]]:
        """Evidence for several claims in one round trip, keyed by claim."""
        if not claim_ids:
            return {}
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    claim_evidence.select()
                    .where(claim_evidence.c.claim_id.in_(claim_ids))
                    .order_by(claim_evidence.c.claim_id, claim_evidence.c.ordinal)
                )
            ).all()
        grouped: dict[UUID, list[ClaimEvidence]] = {}
        for row in rows:
            evidence = ClaimEvidence.model_validate(row._mapping)
            grouped.setdefault(evidence.claim_id, []).append(evidence)
        return grouped

    async def vouches_for_run(self, run_id: UUID) -> list[VouchEdge]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    vouch_edges.select()
                    .where(vouch_edges.c.run_id == run_id)
                    .order_by(vouch_edges.c.created_at, vouch_edges.c.id)
                )
            ).all()
        return [VouchEdge.model_validate(row._mapping) for row in rows]

    async def vouches_for_claim(self, claim_id: UUID) -> list[VouchEdge]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    vouch_edges.select()
                    .where(vouch_edges.c.claim_id == claim_id)
                    .order_by(vouch_edges.c.created_at, vouch_edges.c.id)
                )
            ).all()
        return [VouchEdge.model_validate(row._mapping) for row in rows]
