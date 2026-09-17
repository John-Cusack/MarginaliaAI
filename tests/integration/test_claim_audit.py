"""Mechanical claim audit rules over the real Postgres schema."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGClaimRepo,
    PGDocumentTextRepo,
    PGEntityRepo,
)
from research_engine.adapters.storage.postgres.schema import anchors, claim_edges, claims, entities
from research_engine.domain.claims import (
    CLAIM_AUDIT_ASSURANCE,
    AnchorDraft,
    AnchorRole,
    AnchorVerifyStatus,
    ClaimDraft,
    ClaimKind,
    ClaimRelation,
)
from research_engine.domain.entities import EntityDraft
from research_engine.mcp.dispatch import dispatch_tool
from research_engine.services.argument import ClaimAuditService, ClaimService
from research_engine.testing.corpus import new_id

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.domain.claims import Claim
    from research_engine.domain.spans import SourceSpan
    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

TEXT = "The attributed sentence supports this audit fixture."


async def _evidence(
    engine: AsyncEngine, corpus: Corpus
) -> tuple[SourceSpan, UUID]:
    document_id = await corpus.add_document(title="Claim audit source")
    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(
            tx, document_id, TEXT, "test", "claim-audit-1"
        )
        person = await PGEntityRepo(engine).insert(
            tx,
            EntityDraft(entity_type="person", canonical_name="An Auditor"),
        )
    corpus.track(entities, person.id)
    span = await corpus.add_span(document_id, 0, len(TEXT))
    return span, person.id


async def _add_anchor(
    engine: AsyncEngine,
    claim: Claim,
    span: SourceSpan,
    *,
    role: AnchorRole,
    person_id: UUID | None = None,
) -> None:
    async with transaction(engine) as tx:
        await PGClaimRepo(engine).add_anchor(
            tx,
            claim.id,
            AnchorDraft(
                role=role,
                quoted_text=TEXT,
                source_span_id=span.id,
                person_entity_id=person_id,
                verify_status=AnchorVerifyStatus.EXACT,
                verified_at=datetime.now(UTC),
                parser_version="claim-audit-1",
            ),
        )


async def _upsert(
    engine: AsyncEngine,
    corpus: Corpus,
    ref: str,
    *,
    kind: ClaimKind = ClaimKind.PREMISE,
    public_ready: bool = False,
) -> Claim:
    async with transaction(engine) as tx:
        claim = await PGClaimRepo(engine).upsert_claim(
            tx,
            ClaimDraft(
                ref=ref,
                statement=f"Statement for {ref}.",
                kind=kind,
                public_ready=public_ready,
            ),
        )
    corpus.adopt_claim(claim.id)
    return claim


@pytest.mark.asyncio
async def test_opposition_needs_an_asserts_anchor(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    claim = await corpus.add_claim("AUDIT-OPPOSITION", kind=ClaimKind.OPPOSITION)
    span, person_id = await _evidence(engine, corpus)
    await _add_anchor(engine, claim, span, role=AnchorRole.SUPPORTS)
    repo = PGClaimRepo(engine)

    report = await repo.audit([claim.ref])
    assert [finding.rule_id for finding in report.findings] == [
        "CLM_OPPOSITION_UNANCHORED"
    ]

    await _add_anchor(
        engine, claim, span, role=AnchorRole.ASSERTS, person_id=person_id
    )
    assert (await repo.audit([claim.ref])).findings == []


@pytest.mark.asyncio
async def test_rebuttal_target_needs_a_person_attributed_assertion(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    source = await corpus.add_claim("AUDIT-REBUTTER", kind=ClaimKind.MINE)
    target = await corpus.add_claim("AUDIT-TARGET", kind=ClaimKind.PREMISE)
    span, person_id = await _evidence(engine, corpus)
    repo = PGClaimRepo(engine)
    async with transaction(engine) as tx:
        await repo.add_edge(
            tx, source.id, target.id, ClaimRelation.REBUTS
        )

    report = await repo.audit([source.ref])
    assert [finding.rule_id for finding in report.findings] == [
        "CLM_REBUTS_UNANCHORED"
    ]
    assert report.findings[0].detail == {
        "target_ref": target.ref,
        "target_statement": target.statement,
    }

    async with transaction(engine) as tx:
        await tx.conn.execute(
            anchors.insert().values(
                id=new_id(),
                claim_id=target.id,
                role="asserts",
                person_entity_id=None,
                source_span_id=span.id,
                quoted_text=TEXT,
                verify_status="exact",
                verified_at=datetime.now(UTC),
                parser_version="claim-audit-1",
                locator={},
            )
        )
    assert [
        finding.rule_id for finding in (await repo.audit([source.ref])).findings
    ] == ["CLM_REBUTS_UNANCHORED"]

    await _add_anchor(
        engine, target, span, role=AnchorRole.ASSERTS, person_id=person_id
    )
    assert (await repo.audit([source.ref])).findings == []


@pytest.mark.asyncio
async def test_public_ready_requires_mechanical_rules_to_be_clear(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    claim = await _upsert(
        engine,
        corpus,
        "AUDIT-PUBLIC",
        kind=ClaimKind.OPPOSITION,
        public_ready=True,
    )
    repo = PGClaimRepo(engine)

    report = await repo.audit([claim.ref])
    assert [finding.rule_id for finding in report.findings] == [
        "CLM_OPPOSITION_UNANCHORED",
        "CLM_PUBLIC_UNEARNED",
    ]

    async with transaction(engine) as tx:
        await repo.upsert_claim(
            tx,
            ClaimDraft(
                ref=claim.ref,
                statement=claim.statement,
                kind=claim.kind,
                public_ready=False,
            ),
        )
    assert [finding.rule_id for finding in (await repo.audit([claim.ref])).findings] == [
        "CLM_OPPOSITION_UNANCHORED"
    ]


@pytest.mark.asyncio
async def test_ref_scope_and_clean_assurance(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    first = await corpus.add_claim("AUDIT-SCOPE-A", kind=ClaimKind.OPPOSITION)
    second = await corpus.add_claim("AUDIT-SCOPE-B", kind=ClaimKind.OPPOSITION)
    clean = await corpus.add_claim("AUDIT-CLEAN", kind=ClaimKind.PREMISE)
    service = ClaimAuditService(PGClaimRepo(engine))

    scoped = await service.audit([first.ref])
    assert {finding.claim_ref for finding in scoped.findings} == {first.ref}
    assert second.ref not in {finding.claim_ref for finding in scoped.findings}

    response = await dispatch_tool(
        SimpleNamespace(claim_audit_service=service),
        "claim_audit",
        {"refs": [clean.ref]},
    )
    assert response == {
        "findings": [],
        "checked_refs": [clean.ref],
        "assurance": CLAIM_AUDIT_ASSURANCE,
    }

    invalid = await dispatch_tool(
        SimpleNamespace(claim_audit_service=service),
        "claim_audit",
        {"refs": [" "]},
    )
    assert invalid["error"]["code"] == "invalid_input"

    missing = await dispatch_tool(
        SimpleNamespace(claim_audit_service=service),
        "claim_audit",
        {"refs": ["NO-SUCH-CLAIM"]},
    )
    assert missing["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_claim_upsert_audits_after_commit(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    repo = PGClaimRepo(engine)
    audit = ClaimAuditService(repo)
    service = ClaimService(
        claims_repo=repo,
        spans_repo=SimpleNamespace(),
        editions_repo=SimpleNamespace(),
        entity_service=SimpleNamespace(),
        verification=SimpleNamespace(),
        audit=audit,
        transaction_factory=partial(transaction, engine),
    )

    response = await dispatch_tool(
        SimpleNamespace(claim_service=service),
        "claim_upsert",
        {
            "ref": "AUDIT-WRITE",
            "statement": "The persisted claim is audited after commit.",
            "kind": "opposition",
        },
    )
    assert [finding["rule_id"] for finding in response["findings"]] == [
        "CLM_OPPOSITION_UNANCHORED"
    ]
    stored = await repo.get_by_ref("AUDIT-WRITE")
    assert stored is not None
    corpus.adopt_claim(stored.id)


@pytest.mark.asyncio
async def test_audit_does_not_mutate_ledger_rows(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    source = await _upsert(
        engine,
        corpus,
        "AUDIT-IMMUTABLE-SOURCE",
        kind=ClaimKind.MINE,
        public_ready=True,
    )
    target = await corpus.add_claim(
        "AUDIT-IMMUTABLE-TARGET", kind=ClaimKind.PREMISE
    )
    span, _person_id = await _evidence(engine, corpus)
    await _add_anchor(engine, source, span, role=AnchorRole.SUPPORTS)
    repo = PGClaimRepo(engine)
    async with transaction(engine) as tx:
        await repo.add_edge(tx, source.id, target.id, ClaimRelation.REBUTS)

    async def snapshot() -> tuple[list[tuple], list[tuple], list[tuple]]:
        async with engine.connect() as conn:
            claim_rows = (
                await conn.execute(
                    sa.select(
                        claims.c.id,
                        claims.c.status,
                        claims.c.public_ready,
                    )
                    .where(claims.c.id.in_([source.id, target.id]))
                    .order_by(claims.c.id)
                )
            ).all()
            edge_rows = (
                await conn.execute(
                    sa.select(
                        claim_edges.c.id,
                        claim_edges.c.source_id,
                        claim_edges.c.target_id,
                        claim_edges.c.relation,
                    ).where(claim_edges.c.source_id == source.id)
                )
            ).all()
            anchor_rows = (
                await conn.execute(
                    sa.select(
                        anchors.c.id,
                        anchors.c.claim_id,
                        anchors.c.role,
                        anchors.c.source_span_id,
                    ).where(anchors.c.claim_id == source.id)
                )
            ).all()
        return claim_rows, edge_rows, anchor_rows

    before = await snapshot()
    await repo.audit()
    assert await snapshot() == before
