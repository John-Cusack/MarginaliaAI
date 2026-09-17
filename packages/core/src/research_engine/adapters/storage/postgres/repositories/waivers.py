"""Waivers — rows that let a gated finding pass, never flags.

A waiver names its rule, its subject (a citation key, a block key, or nothing
for the whole revision), and the human who answers for it. Freeze inserts the
waivers it was given in the same transaction it freezes in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.repositories.authored import require_draft
from research_engine.adapters.storage.postgres.schema import waivers
from research_engine.domain.works import Waiver, WaiverDraft

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGWaiverRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, tx: Transaction, draft: WaiverDraft) -> Waiver:
        await require_draft(tx.conn, draft.revision_id)
        row = (
            await tx.conn.execute(
                waivers.insert()
                .values(
                    id=uuid7(),
                    revision_id=draft.revision_id,
                    rule_id=draft.rule_id,
                    subject=draft.subject,
                    actor=draft.actor,
                    reason=draft.reason,
                )
                .returning(waivers)
            )
        ).first()
        assert row is not None
        return self._to_domain(row)

    async def for_revision(self, revision_id: UUID) -> list[Waiver]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    waivers.select()
                    .where(waivers.c.revision_id == revision_id)
                    .order_by(waivers.c.created_at)
                )
            ).all()
        return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_domain(row: Any) -> Waiver:
        return Waiver(
            id=row.id,
            revision_id=row.revision_id,
            rule_id=row.rule_id,
            subject=row.subject,
            actor=row.actor,
            reason=row.reason,
            created_at=row.created_at,
        )
