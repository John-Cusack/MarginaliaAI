"""Postgres persistence for the claim ledger."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import anchors, claim_edges, claims
from research_engine.domain.claims import (
    CLAIM_AUDIT_ASSURANCE,
    Anchor,
    Claim,
    ClaimAuditReport,
    ClaimEdge,
    ClaimFinding,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.domain.claims import AnchorDraft, ClaimDraft, ClaimRelation
    from research_engine.ports.repositories import Transaction


class PGClaimRepo:
    """Claim writes participate in the caller's transaction; reads do not."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert_claim(self, tx: Transaction, draft: ClaimDraft) -> Claim:
        values = {
            "id": uuid7(),
            "ref": draft.ref,
            "statement": draft.statement,
            "kind": draft.kind.value,
            "status": draft.status.value,
            "confidence": draft.confidence,
            "steelman": draft.steelman,
            "public_ready": draft.public_ready,
            "academic_candidate": draft.academic_candidate,
            "attributes": draft.attributes,
        }
        stmt = pg_insert(claims).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[claims.c.ref],
            set_={
                "statement": stmt.excluded.statement,
                "kind": stmt.excluded.kind,
                "status": stmt.excluded.status,
                "confidence": stmt.excluded.confidence,
                "steelman": stmt.excluded.steelman,
                "public_ready": stmt.excluded.public_ready,
                "academic_candidate": stmt.excluded.academic_candidate,
                "attributes": stmt.excluded.attributes,
                "updated_at": sa.func.now(),
            },
        ).returning(claims)
        row = (await tx.conn.execute(stmt)).one()
        return self._claim(row)

    async def add_edge(
        self,
        tx: Transaction,
        source_id: UUID,
        target_id: UUID,
        relation: ClaimRelation,
        confidence: float | None = None,
        note: str | None = None,
    ) -> ClaimEdge:
        values = {
            "id": uuid7(),
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation.value,
            "confidence": confidence,
            "note": note,
        }
        stmt = pg_insert(claim_edges).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                claim_edges.c.source_id,
                claim_edges.c.target_id,
                claim_edges.c.relation,
            ],
            set_={
                "confidence": stmt.excluded.confidence,
                "note": stmt.excluded.note,
            },
        ).returning(claim_edges)
        row = (await tx.conn.execute(stmt)).one()
        return self._edge(row)

    async def add_anchor(
        self, tx: Transaction, claim_id: UUID, draft: AnchorDraft
    ) -> Anchor:
        row = (
            await tx.conn.execute(
                anchors.insert()
                .values(
                    id=uuid7(),
                    claim_id=claim_id,
                    role=draft.role.value,
                    person_entity_id=draft.person_entity_id,
                    source_span_id=draft.source_span_id,
                    quoted_text=draft.quoted_text,
                    verify_status=draft.verify_status.value,
                    verified_at=draft.verified_at,
                    parser_version=draft.parser_version,
                    edition_id=draft.edition_id,
                    edition_key=draft.edition_key,
                    locator=draft.locator,
                )
                .returning(anchors)
            )
        ).one()
        return self._anchor(row)

    async def get_by_ref(self, ref: str) -> Claim | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(claims.select().where(claims.c.ref == ref))
            ).first()
        return self._claim(row) if row else None

    async def existing_refs(self, refs: list[str]) -> set[str]:
        """Return the requested refs that exist, in one query."""
        if not refs:
            return set()
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(claims.c.ref).where(claims.c.ref.in_(set(refs)))
                )
            ).scalars()
            return set(rows)

    async def anchors_for(self, claim_id: UUID) -> list[Anchor]:
        stmt = (
            anchors.select()
            .where(anchors.c.claim_id == claim_id)
            .order_by(anchors.c.created_at, anchors.c.id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [self._anchor(row) for row in rows]

    async def anchor_by_id(self, anchor_id: UUID) -> Anchor | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(anchors.select().where(anchors.c.id == anchor_id))
            ).first()
        return self._anchor(row) if row else None

    async def edges_for(self, claim_id: UUID) -> list[ClaimEdge]:
        stmt = (
            claim_edges.select()
            .where(
                sa.or_(
                    claim_edges.c.source_id == claim_id,
                    claim_edges.c.target_id == claim_id,
                )
            )
            .order_by(claim_edges.c.created_at, claim_edges.c.id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [self._edge(row) for row in rows]

    async def audit(self, refs: list[str] | None = None) -> ClaimAuditReport:
        """Run the ledger's mechanical checks without changing any row."""
        subject_scope = sa.true() if refs is None else claims.c.ref.in_(refs)
        assertion = sa.exists(
            sa.select(1).where(
                anchors.c.claim_id == claims.c.id,
                anchors.c.role == "asserts",
            )
        )
        opposition_rows = (
            await self._fetch_all(
                sa.select(claims.c.ref, claims.c.statement).where(
                    subject_scope,
                    claims.c.kind == "opposition",
                    ~assertion,
                )
            )
        )

        source = claims.alias("source_claim")
        target = claims.alias("target_claim")
        target_assertion = sa.exists(
            sa.select(1).where(
                anchors.c.claim_id == target.c.id,
                anchors.c.role == "asserts",
                anchors.c.person_entity_id.is_not(None),
            )
        )
        rebuttal_rows = (
            await self._fetch_all(
                sa.select(
                    source.c.ref.label("claim_ref"),
                    target.c.ref.label("target_ref"),
                    target.c.statement.label("target_statement"),
                )
                .select_from(
                    claim_edges.join(source, source.c.id == claim_edges.c.source_id).join(
                        target, target.c.id == claim_edges.c.target_id
                    )
                )
                .where(
                    source.c.ref.in_(refs) if refs is not None else sa.true(),
                    claim_edges.c.relation == "rebuts",
                    ~target_assertion,
                )
            )
        )

        findings = [
            ClaimFinding(
                rule_id="CLM_OPPOSITION_UNANCHORED",
                severity="error",
                claim_ref=row.ref,
                message="Opposition claim has no asserts anchor.",
                detail={"statement": row.statement},
            )
            for row in opposition_rows
        ]
        findings.extend(
            ClaimFinding(
                rule_id="CLM_REBUTS_UNANCHORED",
                severity="error",
                claim_ref=row.claim_ref,
                message="Rebuttal targets a claim with no person-attributed asserts anchor.",
                detail={
                    "target_ref": row.target_ref,
                    "target_statement": row.target_statement,
                },
            )
            for row in rebuttal_rows
        )

        open_rules: dict[str, set[str]] = {}
        for finding in findings:
            open_rules.setdefault(finding.claim_ref, set()).add(finding.rule_id)
        if open_rules:
            public_rows = await self._fetch_all(
                sa.select(claims.c.ref).where(
                    claims.c.ref.in_(open_rules),
                    claims.c.public_ready.is_(True),
                )
            )
            findings.extend(
                ClaimFinding(
                    rule_id="CLM_PUBLIC_UNEARNED",
                    severity="error",
                    claim_ref=row.ref,
                    message="Claim is public-ready while a mechanical claim rule is open.",
                    detail={"open_rules": sorted(open_rules[row.ref])},
                )
                for row in public_rows
            )

        findings.sort(key=lambda finding: (finding.claim_ref, finding.rule_id))
        return ClaimAuditReport(
            findings=findings,
            checked_refs=list(refs) if refs is not None else None,
            assurance=CLAIM_AUDIT_ASSURANCE,
        )

    async def _fetch_all(self, statement: Any) -> list[Any]:
        async with self._engine.connect() as conn:
            return list((await conn.execute(statement)).all())

    @staticmethod
    def _claim(row: Any) -> Claim:
        return Claim(
            id=row.id,
            ref=row.ref,
            statement=row.statement,
            kind=row.kind,
            status=row.status,
            confidence=row.confidence,
            steelman=row.steelman,
            public_ready=row.public_ready,
            academic_candidate=row.academic_candidate,
            attributes=row.attributes or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _edge(row: Any) -> ClaimEdge:
        return ClaimEdge(
            id=row.id,
            source_id=row.source_id,
            target_id=row.target_id,
            relation=row.relation,
            confidence=row.confidence,
            note=row.note,
            created_at=row.created_at,
        )

    @staticmethod
    def _anchor(row: Any) -> Anchor:
        return Anchor(
            id=row.id,
            claim_id=row.claim_id,
            role=row.role,
            person_entity_id=row.person_entity_id,
            source_span_id=row.source_span_id,
            quoted_text=row.quoted_text,
            verify_status=row.verify_status,
            verified_at=row.verified_at,
            parser_version=row.parser_version,
            edition_id=row.edition_id,
            edition_key=row.edition_key,
            locator=row.locator or {},
            created_at=row.created_at,
        )
