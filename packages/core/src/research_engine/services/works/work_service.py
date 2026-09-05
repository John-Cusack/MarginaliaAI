"""Works and blocks as rows — create, read, upsert, link, archive.

`work_create` makes the work and its revision 1 in one transaction;
`work_get` returns the assembled tree Appendix B promises; `upsert_block`
inserts by key or updates under optimistic locking (the tool maps a stale
write to `conflict`); `link` types one edge to the corpus. Every writer
refuses a revision that is not a draft — edits to history go through
copy-forward, never through these methods.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

import structlog
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from uuid_utils import uuid7

from research_engine.domain.errors import (
    FrozenRevisionError,
    NotFoundError,
)
from research_engine.domain.works import (
    BlockEntityLinkDraft,
    BlockSourceLinkDraft,
    Work,
    WorkBlockDraft,
    WorkDraft,
    WorkRevision,
    WorkRevisionDraft,
)
from research_engine.services.works.assembly import AssembledRevision, assemble_revision

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger()


class WorkCreated(BaseModel):
    work_id: UUID
    slug: str
    revision_id: UUID
    revision_number: int
    state: str


class BlockWritten(BaseModel):
    block_id: UUID
    block_key: UUID
    updated_at: Any = None


class LinkWritten(BaseModel):
    kind: str
    target_id: UUID
    relation: str


class WorkService:
    """The drafting-loop writers and the `work_get` reader."""

    def __init__(
        self,
        *,
        works: Any,
        revisions: Any,
        blocks: Any,
        citations: Any,
        links: Any,
        spans: Any,
        transaction_factory: Callable[[], Any],
    ) -> None:
        self._works = works
        self._revisions = revisions
        self._blocks = blocks
        self._citations = citations
        self._links = links
        self._spans = spans
        self._transaction = transaction_factory

    async def create(
        self,
        *,
        slug: str,
        title: str,
        work_type: str,
        language: str | None = None,
        abstract: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkCreated:
        """Create the work and its revision 1, both draft, in one transaction."""
        try:
            async with self._transaction() as tx:
                work = await self._works.insert(
                    tx,
                    WorkDraft(
                        slug=slug,
                        title=title,
                        work_type=work_type,
                        language=language,
                        abstract=abstract,
                        metadata=metadata or {},
                    ),
                )
                revision = await self._revisions.insert(
                    tx, WorkRevisionDraft(work_id=work.id, revision_number=1)
                )
                await self._works.set_current_revision(tx, work.id, revision.id)
        except IntegrityError as exc:
            raise ValueError(f"slug {slug!r} is taken") from exc
        logger.info(
            "work_created", slug=slug, work_id=str(work.id),
            revision_id=str(revision.id),
        )
        return WorkCreated(
            work_id=work.id,
            slug=work.slug,
            revision_id=revision.id,
            revision_number=revision.revision_number,
            state=revision.state.value,
        )

    async def get(
        self, *, slug: str | None = None, work_id: UUID | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """The ordered block tree with occurrences, items, and links inlined."""
        work, resolved = await self._resolve_revision(
            slug=slug, work_id=work_id, revision_number=revision
        )
        assembled = await assemble_revision(
            work,
            resolved,
            blocks=self._blocks,
            citations=self._citations,
            links=self._links,
            spans=self._spans,
        )
        return _dump_assembled(assembled)

    async def upsert_block(
        self,
        *,
        slug: str,
        position: int,
        block_type: str,
        body_markdown: str,
        block_key: UUID | None = None,
        parent_key: UUID | None = None,
        title: str | None = None,
        attributes: dict[str, Any] | None = None,
        expected_updated_at: Any = None,
    ) -> BlockWritten:
        """Insert a block by fresh key, or update one under optimistic locking."""
        work = await self._require_work(slug=slug)
        revision = await self._draft_revision(work)
        async with self._transaction() as tx:
            parent_id = None
            if parent_key is not None:
                parent = await self._blocks.by_key_in_tx(
                    tx, revision.id, parent_key
                )
                if parent is None:
                    raise NotFoundError("work_block", parent_key)
                parent_id = parent.id
            block = await self._blocks.upsert(
                tx,
                revision.id,
                WorkBlockDraft(
                    revision_id=revision.id,
                    # uuid_utils ids never cross into pydantic: drafts take
                    # stdlib UUIDs, while the repos keep uuid7 for row ids.
                    block_key=block_key or UUID(str(uuid7())),
                    parent_id=parent_id,
                    position=position,
                    block_type=block_type,
                    title=title,
                    body_markdown=body_markdown,
                    attributes=attributes or {},
                ),
                expected_updated_at=expected_updated_at,
            )
        return BlockWritten(
            block_id=block.id, block_key=block.block_key,
            updated_at=block.updated_at,
        )

    async def link(
        self,
        *,
        slug: str,
        block_key: UUID,
        relation: str,
        source_span_id: UUID | None = None,
        document_id: UUID | None = None,
        char_start: int | None = None,
        char_end: int | None = None,
        entity_id: UUID | None = None,
        confidence: float | None = None,
        note: str | None = None,
    ) -> LinkWritten:
        """Type one edge from a block to a span (by id or coordinates) or an entity."""
        targets = [
            source_span_id is not None,
            document_id is not None
            and char_start is not None
            and char_end is not None,
            entity_id is not None,
        ]
        if sum(targets) != 1:
            raise ValueError(
                "link needs exactly one target: source_span_id, "
                "(document_id, char_start, char_end), or entity_id"
            )
        work = await self._require_work(slug=slug)
        revision = await self._draft_revision(work)
        block = await self._blocks.by_key(revision.id, block_key)
        if block is None:
            raise NotFoundError("work_block", block_key)
        async with self._transaction() as tx:
            if entity_id is not None:
                link = await self._links.add_entity_link(
                    tx,
                    BlockEntityLinkDraft(
                        block_id=block.id,
                        entity_id=entity_id,
                        relation=relation,
                        surface_form=None,
                    ),
                )
                return LinkWritten(
                    kind="entity", target_id=link.entity_id,
                    relation=link.relation,
                )
            span_id = source_span_id
            if span_id is None:
                assert document_id is not None  # narrowed by the target count
                assert char_start is not None and char_end is not None
                span = await self._spans.resolve(
                    tx,
                    document_id=document_id,
                    char_start=char_start,
                    char_end=char_end,
                )
                span_id = span.id
            link = await self._links.add_source_link(
                tx,
                BlockSourceLinkDraft(
                    block_id=block.id,
                    source_span_id=span_id,
                    relation=relation,
                    confidence=confidence,
                    note=note,
                ),
            )
        return LinkWritten(
            kind="source", target_id=link.source_span_id,
            relation=link.relation,
        )

    async def archive(self, *, slug: str) -> Work:
        work = await self._require_work(slug=slug)
        async with self._transaction() as tx:
            return await self._works.archive(tx, work.id)

    async def _require_work(
        self, *, slug: str | None = None, work_id: UUID | None = None
    ) -> Work:
        if slug is not None:
            work = await self._works.get_by_slug(slug)
        elif work_id is not None:
            work = await self._works.get(work_id)
        else:
            work = None
        if work is None:
            raise NotFoundError("work", slug or work_id)
        return work

    async def _draft_revision(self, work: Work) -> WorkRevision:
        if work.current_revision_id is None:
            raise NotFoundError("work_revision", f"current of {work.slug}")
        revision = await self._revisions.get(work.current_revision_id)
        if revision is None:
            raise NotFoundError("work_revision", work.current_revision_id)
        if revision.state.value != "draft":
            raise FrozenRevisionError(
                f"Revision {revision.id} is {revision.state.value}, not draft: "
                "copy it forward to edit."
            )
        return revision

    async def _resolve_revision(
        self,
        *,
        slug: str | None,
        work_id: UUID | None,
        revision_number: int | None,
    ) -> tuple[Work, WorkRevision]:
        work = await self._require_work(slug=slug, work_id=work_id)
        if revision_number is None:
            if work.current_revision_id is None:
                raise NotFoundError("work_revision", f"current of {work.slug}")
            revision = await self._revisions.get(work.current_revision_id)
            if revision is None:
                raise NotFoundError("work_revision", work.current_revision_id)
            return work, revision
        latest = await self._revisions.latest(work.id)
        if latest is None or revision_number > latest.revision_number:
            raise NotFoundError(
                "work_revision", f"{work.slug} revision {revision_number}"
            )
        current = latest
        while current.revision_number != revision_number:
            if current.parent_revision_id is None:
                raise NotFoundError(
                    "work_revision", f"{work.slug} revision {revision_number}"
                )
            parent = await self._revisions.get(current.parent_revision_id)
            if parent is None:  # pragma: no cover - FK keeps the chain whole
                raise NotFoundError(
                    "work_revision", f"{work.slug} revision {revision_number}"
                )
            current = parent
        return work, current


def _dump_assembled(view: AssembledRevision) -> dict[str, Any]:
    """The `work_get` shape: JSON-safe, keys as strings, datetimes ISO."""
    work = view.work.model_dump(mode="json")
    revision = view.revision.model_dump(mode="json")
    revision["content_hash"] = (
        view.revision.content_hash.hex() if view.revision.content_hash else None
    )
    blocks: list[dict[str, Any]] = []
    for item in view.blocks:
        citations = []
        for entry in item.citations:
            citations.append({
                "citation_key": str(entry.occurrence.citation_key),
                "intent": entry.occurrence.intent.value,
                "placement": entry.occurrence.placement.value,
                "items": [
                    {
                        "position": row.position,
                        "zotero_key": row.zotero_key,
                        "edition_id": str(row.edition_id)
                        if row.edition_id is not None
                        else None,
                        "source_span_id": str(row.source_span_id)
                        if row.source_span_id is not None
                        else None,
                        "quoted_text": row.quoted_text,
                        "verify_status": row.verify_status,
                        "verified_at": row.verified_at.isoformat()
                        if row.verified_at is not None
                        else None,
                        "locator": dict(row.locator),
                    }
                    for row in entry.items
                ],
            })
        links = [
            {
                "kind": "source",
                "target_id": str(link.source_span_id),
                "relation": link.relation,
                "confidence": link.confidence,
            }
            for link in (item.links.sources if item.links else [])
        ] + [
            {
                "kind": "entity",
                "target_id": str(link.entity_id),
                "relation": link.relation,
                "confidence": None,
            }
            for link in (item.links.entities if item.links else [])
        ]
        block = item.block.model_dump(mode="json")
        block["parent_key"] = (
            str(item.parent_key) if item.parent_key is not None else None
        )
        block.pop("parent_id", None)
        block["citations"] = citations
        block["links"] = links
        blocks.append(block)
    return {"work": work, "revision": revision, "blocks": blocks}


__all__ = [
    "BlockWritten",
    "LinkWritten",
    "WorkCreated",
    "WorkService",
]
