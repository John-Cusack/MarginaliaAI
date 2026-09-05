"""Works and their revisions — identity, lifecycle, and the copy-forward.

A work is a slug with a current revision; a revision is a numbered snapshot in
a state machine (draft → frozen → published, with superseded for the
replaced). Content writes never touch a non-draft revision — edits copy
forward into a new draft, so history is append-only and the frozen hash stays
meaningful.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import (
    block_entity_links,
    block_source_links,
    citation_items,
    citation_occurrences,
    work_blocks,
    work_revisions,
    works,
)
from research_engine.domain.errors import FrozenRevisionError, NotFoundError
from research_engine.domain.works import (
    RevisionState,
    Work,
    WorkDraft,
    WorkRevision,
    WorkRevisionDraft,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


async def require_draft(conn: Any, revision_id: UUID) -> None:
    """Refuse content writes to anything but a draft revision."""
    state = (
        await conn.execute(
            sa.select(work_revisions.c.state).where(
                work_revisions.c.id == revision_id
            )
        )
    ).scalar_one_or_none()
    if state is None:
        raise NotFoundError("work_revision", revision_id)
    if state != RevisionState.DRAFT.value:
        raise FrozenRevisionError(
            f"Revision {revision_id} is {state}, not draft: copy it forward to edit."
        )


class PGWorkRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, tx: Transaction, draft: WorkDraft) -> Work:
        row = (
            await tx.conn.execute(
                works.insert()
                .values(
                    id=uuid7(),
                    slug=draft.slug,
                    title=draft.title,
                    work_type=draft.work_type,
                    language=draft.language,
                    abstract=draft.abstract,
                    metadata=draft.metadata,
                )
                .returning(works)
            )
        ).first()
        assert row is not None
        return self._to_domain(row)

    async def get(self, work_id: UUID) -> Work | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(works.select().where(works.c.id == work_id))
            ).first()
            return self._to_domain(row) if row else None

    async def get_by_slug(self, slug: str) -> Work | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(works.select().where(works.c.slug == slug))
            ).first()
            return self._to_domain(row) if row else None

    async def list(self) -> list[Work]:
        """Every work, oldest first — for trace-by-key fan-out."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(works.select().order_by(works.c.created_at))
            ).all()
            return [self._to_domain(row) for row in rows]

    async def set_current_revision(
        self, tx: Transaction, work_id: UUID, revision_id: UUID
    ) -> None:
        await tx.conn.execute(
            works.update()
            .where(works.c.id == work_id)
            .values(current_revision_id=revision_id, updated_at=sa.func.now())
        )

    async def update(
        self, tx: Transaction, work_id: UUID, *, expected_updated_at: Any, **fields: Any
    ) -> Work:
        """Update work fields under optimistic locking.

        Raises:
            StaleWriteError: `expected_updated_at` is missing or does not match
                the stored `updated_at`.
        """
        from research_engine.domain.errors import StaleWriteError

        current = (
            await tx.conn.execute(
                sa.select(works.c.updated_at).where(works.c.id == work_id)
            )
        ).scalar_one_or_none()
        if current is None:
            raise NotFoundError("work", work_id)
        if expected_updated_at is None or str(current) != str(expected_updated_at):
            raise StaleWriteError(
                f"Work {work_id} changed under you: re-read and retry."
            )
        row = (
            await tx.conn.execute(
                works.update()
                .where(works.c.id == work_id)
                .values(updated_at=sa.func.now(), **fields)
                .returning(works)
            )
        ).first()
        assert row is not None
        return self._to_domain(row)

    async def archive(self, tx: Transaction, work_id: UUID) -> Work:
        row = (
            await tx.conn.execute(
                works.update()
                .where(works.c.id == work_id)
                .values(
                    status="archived",
                    archived_at=sa.func.now(),
                    updated_at=sa.func.now(),
                )
                .returning(works)
            )
        ).first()
        if row is None:
            raise NotFoundError("work", work_id)
        return self._to_domain(row)

    @staticmethod
    def _to_domain(row: Any) -> Work:
        return Work(
            id=row.id,
            slug=row.slug,
            title=row.title,
            work_type=row.work_type,
            status=row.status,
            language=row.language,
            abstract=row.abstract,
            current_revision_id=row.current_revision_id,
            metadata=dict(row.metadata or {}),
            created_at=row.created_at,
            updated_at=row.updated_at,
            archived_at=row.archived_at,
        )


class PGWorkRevisionRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, tx: Transaction, draft: WorkRevisionDraft) -> WorkRevision:
        row = (
            await tx.conn.execute(
                work_revisions.insert()
                .values(
                    id=uuid7(),
                    work_id=draft.work_id,
                    revision_number=draft.revision_number,
                    parent_revision_id=draft.parent_revision_id,
                    message=draft.message,
                    created_by=draft.created_by,
                    metadata=draft.metadata,
                )
                .returning(work_revisions)
            )
        ).first()
        assert row is not None
        return self._to_domain(row)

    async def get(self, revision_id: UUID) -> WorkRevision | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    work_revisions.select().where(work_revisions.c.id == revision_id)
                )
            ).first()
            return self._to_domain(row) if row else None

    async def latest(self, work_id: UUID) -> WorkRevision | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    work_revisions.select()
                    .where(work_revisions.c.work_id == work_id)
                    .order_by(work_revisions.c.revision_number.desc())
                    .limit(1)
                )
            ).first()
            return self._to_domain(row) if row else None

    async def copy_forward(self, tx: Transaction, revision_id: UUID) -> WorkRevision:
        """A new draft revision carrying the old one's content under new ids.

        Blocks, occurrences, items, and links are copied with the same
        `block_key`/`citation_key`; waivers are not — a new draft earns its
        own. The source revision is untouched; lifecycle transitions are
        explicit calls, not side effects.
        """
        source = await self._get_in_tx(tx, revision_id)
        if source is None:
            raise NotFoundError("work_revision", revision_id)
        newest = (
            await tx.conn.execute(
                sa.select(sa.func.max(work_revisions.c.revision_number)).where(
                    work_revisions.c.work_id == source.work_id
                )
            )
        ).scalar_one()
        new_id = uuid7()
        await tx.conn.execute(
            work_revisions.insert().values(
                id=new_id,
                work_id=source.work_id,
                revision_number=(newest or 0) + 1,
                parent_revision_id=source.id,
                message=None,
                created_by=source.created_by,
                metadata=dict(source.metadata),
            )
        )
        # Blocks in two passes: ids first (parents may come later in any
        # order), then the parent remap. Keys stay, ids turn over. The first
        # pass parks every block at a transient negative position, because
        # siblings under different parents routinely share a position and
        # the (revision, parent, position) uniqueness is checked per row,
        # not deferred — copying them parentless at real positions collides.
        old_blocks = (
            await tx.conn.execute(
                work_blocks.select().where(work_blocks.c.revision_id == source.id)
            )
        ).all()
        id_map: dict[Any, Any] = {}
        for index, block in enumerate(old_blocks):
            new_block_id = uuid7()
            id_map[block.id] = new_block_id
            await tx.conn.execute(
                work_blocks.insert().values(
                    id=new_block_id,
                    revision_id=new_id,
                    block_key=block.block_key,
                    parent_id=None,
                    position=-(index + 1),
                    block_type=block.block_type,
                    title=block.title,
                    body_markdown=block.body_markdown,
                    attributes=dict(block.attributes or {}),
                )
            )
        for block in old_blocks:
            await tx.conn.execute(
                work_blocks.update()
                .where(work_blocks.c.id == id_map[block.id])
                .values(
                    parent_id=id_map[block.parent_id]
                    if block.parent_id is not None
                    else None,
                    position=block.position,
                )
            )
        # Occurrences, items, and links follow their blocks.
        occurrence_map: dict[Any, Any] = {}
        for block_id in [b.id for b in old_blocks]:
            for occurrence in (
                await tx.conn.execute(
                    sa.select(citation_occurrences).where(
                        citation_occurrences.c.block_id == block_id
                    )
                )
            ).all():
                new_occurrence_id = uuid7()
                occurrence_map[occurrence.id] = new_occurrence_id
                await tx.conn.execute(
                    citation_occurrences.insert().values(
                        id=new_occurrence_id,
                        citation_key=occurrence.citation_key,
                        block_id=id_map[block_id],
                        placement=occurrence.placement,
                        intent=occurrence.intent,
                        note=occurrence.note,
                    )
                )
                for item in (
                    await tx.conn.execute(
                        sa.select(citation_items).where(
                            citation_items.c.occurrence_id == occurrence.id
                        )
                    )
                ).all():
                    await tx.conn.execute(
                        citation_items.insert().values(
                            occurrence_id=new_occurrence_id,
                            position=item.position,
                            edition_id=item.edition_id,
                            zotero_key=item.zotero_key,
                            source_span_id=item.source_span_id,
                            quoted_text=item.quoted_text,
                            verify_status=item.verify_status,
                            verified_at=item.verified_at,
                            locator=dict(item.locator or {}),
                            prefix=item.prefix,
                            suffix=item.suffix,
                            suppress_author=item.suppress_author,
                        )
                    )
        for block in old_blocks:
            for link in (
                await tx.conn.execute(
                    sa.select(block_source_links).where(
                        block_source_links.c.block_id == block.id
                    )
                )
            ).all():
                await tx.conn.execute(
                    block_source_links.insert().values(
                        block_id=id_map[block.id],
                        source_span_id=link.source_span_id,
                        relation=link.relation,
                        confidence=link.confidence,
                        note=link.note,
                    )
                )
            for link in (
                await tx.conn.execute(
                    sa.select(block_entity_links).where(
                        block_entity_links.c.block_id == block.id
                    )
                )
            ).all():
                await tx.conn.execute(
                    block_entity_links.insert().values(
                        block_id=id_map[block.id],
                        entity_id=link.entity_id,
                        relation=link.relation,
                        surface_form=link.surface_form,
                    )
                )
        created = await self._get_in_tx(tx, new_id)
        assert created is not None
        return created

    async def set_message(
        self, tx: Transaction, revision_id: UUID, message: str
    ) -> WorkRevision:
        """Record why a revision was sealed, without touching its state."""
        row = (
            await tx.conn.execute(
                work_revisions.update()
                .where(work_revisions.c.id == revision_id)
                .values(message=message)
                .returning(work_revisions)
            )
        ).first()
        if row is None:
            raise NotFoundError("work_revision", revision_id)
        return self._to_domain(row)

    async def freeze(
        self, tx: Transaction, revision_id: UUID, content_hash: bytes
    ) -> WorkRevision:
        return await self._transition(tx, revision_id, "frozen", {"frozen_at": sa.func.now(), "content_hash": content_hash})

    async def publish(self, tx: Transaction, revision_id: UUID) -> WorkRevision:
        return await self._transition(tx, revision_id, "published", {"published_at": sa.func.now()})

    async def supersede(self, tx: Transaction, revision_id: UUID) -> WorkRevision:
        return await self._transition(tx, revision_id, "superseded", {})

    async def _transition(
        self, tx: Transaction, revision_id: UUID, state: str, extra: dict[str, Any]
    ) -> WorkRevision:
        current = await self._get_in_tx(tx, revision_id)
        if current is None:
            raise NotFoundError("work_revision", revision_id)
        allowed = {
            "frozen": (RevisionState.DRAFT.value,),
            "published": (RevisionState.FROZEN.value,),
            "superseded": (
                RevisionState.FROZEN.value,
                RevisionState.PUBLISHED.value,
                RevisionState.DRAFT.value,
            ),
        }
        if current.state not in allowed[state]:
            raise FrozenRevisionError(
                f"Revision {revision_id} is {current.state}: cannot become {state}."
            )
        row = (
            await tx.conn.execute(
                work_revisions.update()
                .where(work_revisions.c.id == revision_id)
                .values(state=state, **extra)
                .returning(work_revisions)
            )
        ).first()
        assert row is not None
        return self._to_domain(row)

    async def _get_in_tx(self, tx: Transaction, revision_id: UUID) -> WorkRevision | None:
        row = (
            await tx.conn.execute(
                work_revisions.select().where(work_revisions.c.id == revision_id)
            )
        ).first()
        return self._to_domain(row) if row else None

    @staticmethod
    def _to_domain(row: Any) -> WorkRevision:
        return WorkRevision(
            id=row.id,
            work_id=row.work_id,
            revision_number=row.revision_number,
            parent_revision_id=row.parent_revision_id,
            state=row.state,
            message=row.message,
            content_hash=row.content_hash,
            created_by=row.created_by,
            created_at=row.created_at,
            frozen_at=row.frozen_at,
            published_at=row.published_at,
            metadata=dict(row.metadata or {}),
        )
