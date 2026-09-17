"""Freeze and publish revisions — the human act that commits.

`freeze` validates at the freeze gate, refuses on blockers, inserts the
waivers it was given, and stores the content hash. `publish` validates at
the publish gate, where edition identity graduates from warning to error.
Both run in one transaction; a refusal writes nothing, waivers included.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

import structlog
from pydantic import BaseModel, Field

from research_engine.domain.errors import NotFoundError
from research_engine.domain.works import WaiverDraft
from research_engine.services.works.assembly import assemble_revision, hash_assembled

if TYPE_CHECKING:
    from collections.abc import Callable

    from research_engine.services.works.validate import WorkValidationService

logger = structlog.get_logger()


class FreezeBlocked(Exception):
    """The revision failed its gate; waivers and the freeze were not written."""

    def __init__(self, blockers: list[str]) -> None:
        super().__init__(
            "Revision is blocked by: " + ", ".join(blockers)
            + ". Pass waivers for the answered ones, or fix the rest."
        )
        self.blockers = blockers


class WaiverGiven(BaseModel):
    rule_id: str
    subject: str | None = None
    reason: str
    actor: str = "user"


class RevisionSealed(BaseModel):
    revision_id: UUID
    revision_number: int
    content_hash: str | None = None
    state: str
    waivers: list[str] = Field(default_factory=list)


class WorkPublicationService:
    """Gate, waive, hash, and seal — the first freeze ends Phase 0."""

    def __init__(
        self,
        *,
        validation: WorkValidationService,
        works: Any,
        revisions: Any,
        blocks: Any,
        citations: Any,
        links: Any,
        spans: Any,
        waivers: Any,
        transaction_factory: Callable[[], Any],
    ) -> None:
        self._validation = validation
        self._works = works
        self._revisions = revisions
        self._blocks = blocks
        self._citations = citations
        self._links = links
        self._spans = spans
        self._waivers = waivers
        self._transaction = transaction_factory

    async def freeze(
        self,
        *,
        slug: str,
        message: str | None = None,
        waivers: list[WaiverGiven] | None = None,
    ) -> RevisionSealed:
        """Validate at freeze, record waivers, hash, and seal the draft."""
        work = await self._works.get_by_slug(slug)
        if work is None:
            raise NotFoundError("work", slug)
        given = waivers or []
        prospective = {(waiver.rule_id, waiver.subject) for waiver in given}
        report = await self._validation.validate(
            slug=slug, gate="freeze", prospective=prospective
        )
        if not report.gate.passed:
            raise FreezeBlocked(report.gate.blockers)
        if work.current_revision_id is None:  # pragma: no cover - validated above
            raise NotFoundError("work_revision", f"current of {slug}")
        revision = await self._revisions.get(work.current_revision_id)
        if revision is None:  # pragma: no cover - validated above
            raise NotFoundError("work_revision", work.current_revision_id)
        view = await assemble_revision(
            work,
            revision,
            blocks=self._blocks,
            citations=self._citations,
            links=self._links,
            spans=self._spans,
        )
        content_hash = hash_assembled(view)
        async with self._transaction() as tx:
            for waiver in given:
                await self._waivers.insert(
                    tx,
                    WaiverDraft(
                        revision_id=revision.id,
                        rule_id=waiver.rule_id,
                        subject=waiver.subject,
                        actor=waiver.actor,
                        reason=waiver.reason,
                    ),
                )
            sealed = await self._revisions.freeze(tx, revision.id, content_hash)
            if message is not None:
                sealed = await self._revisions.set_message(
                    tx, revision.id, message
                )
        logger.info(
            "work_frozen", slug=slug, revision_number=sealed.revision_number,
            content_hash=content_hash.hex(),
        )
        return RevisionSealed(
            revision_id=sealed.id,
            revision_number=sealed.revision_number,
            content_hash=content_hash.hex(),
            state=sealed.state.value,
            waivers=[waiver.rule_id for waiver in given],
        )

    async def publish(self, *, slug: str) -> RevisionSealed:
        """Validate at publish, then seal the frozen revision as published."""
        work = await self._works.get_by_slug(slug)
        if work is None:
            raise NotFoundError("work", slug)
        report = await self._validation.validate(slug=slug, gate="publish")
        if not report.gate.passed:
            raise FreezeBlocked(report.gate.blockers)
        if work.current_revision_id is None:  # pragma: no cover - validated above
            raise NotFoundError("work_revision", f"current of {slug}")
        async with self._transaction() as tx:
            sealed = await self._revisions.publish(tx, work.current_revision_id)
        stored = sealed.content_hash.hex() if sealed.content_hash else None
        logger.info(
            "work_published", slug=slug, revision_number=sealed.revision_number
        )
        return RevisionSealed(
            revision_id=sealed.id,
            revision_number=sealed.revision_number,
            content_hash=stored,
            state=sealed.state.value,
        )
