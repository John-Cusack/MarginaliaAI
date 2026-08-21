"""Document structure tree — reads and writes over `core.document_nodes`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

import sqlalchemy as sa

from research_engine.adapters.storage.postgres.schema import document_nodes
from research_engine.domain.nodes import DocumentNode

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.domain.nodes import DocumentNodeDraft
    from research_engine.ports.repositories import Transaction


def _to_domain(row: Any) -> DocumentNode:
    return DocumentNode(
        id=row.id,
        document_id=row.document_id,
        parent_id=row.parent_id,
        path=str(row.path),
        depth=row.depth,
        position=row.position,
        node_type=row.node_type,
        title=row.title,
        char_start=row.char_start,
        char_end=row.char_end,
        metadata=row.metadata or {},
        created_at=row.created_at,
    )


class PGDocumentNodeRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert_many(
        self, tx: Transaction, document_id: UUID, drafts: list[DocumentNodeDraft]
    ) -> list[DocumentNode]:
        """Insert a whole tree, resolving `parent_path` to `parent_id`.

        Drafts must arrive with every parent ahead of its children — which is
        what `build_node_tree` returns — so each row's parent id is already
        known by the time it is written.
        """
        if not drafts:
            return []

        ids_by_path: dict[str, UUID] = {}
        rows: list[dict[str, Any]] = []
        for draft in drafts:
            node_id = uuid4()
            ids_by_path[draft.path] = node_id
            parent_id = (
                ids_by_path.get(draft.parent_path)
                if draft.parent_path is not None
                else None
            )
            if draft.parent_path is not None and parent_id is None:
                raise ValueError(
                    f"Node {draft.path!r} names parent {draft.parent_path!r}, which "
                    f"has not been inserted. Drafts must be in tree order."
                )
            rows.append(
                {
                    "id": node_id,
                    "document_id": document_id,
                    "parent_id": parent_id,
                    "path": draft.path,
                    "depth": draft.depth,
                    "position": draft.position,
                    "node_type": draft.node_type,
                    "title": draft.title,
                    "char_start": draft.char_start,
                    "char_end": draft.char_end,
                    "metadata": draft.metadata,
                }
            )

        result = await tx.conn.execute(
            document_nodes.insert().returning(document_nodes), rows
        )
        return [_to_domain(row) for row in result]

    async def get(self, node_id: UUID) -> DocumentNode | None:
        """One node, by id."""
        stmt = document_nodes.select().where(document_nodes.c.id == node_id)
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return _to_domain(row) if row else None

    async def get_tree(self, document_id: UUID) -> list[DocumentNode]:
        """Every node of one document, parents before children."""
        stmt = (
            document_nodes.select()
            .where(document_nodes.c.document_id == document_id)
            .order_by(document_nodes.c.path)
        )
        async with self._engine.connect() as conn:
            return [_to_domain(row) for row in await conn.execute(stmt)]

    async def get_outline(
        self, document_id: UUID, *, max_depth: int | None = None
    ) -> list[DocumentNode]:
        """The tree down to *max_depth* — the cheap map an agent reads first.

        Depth-limiting is the whole point: a 900-page book's full node list is
        thousands of rows, while its parts and chapters are a few dozen.
        """
        stmt = document_nodes.select().where(document_nodes.c.document_id == document_id)
        if max_depth is not None:
            stmt = stmt.where(document_nodes.c.depth <= max_depth)
        stmt = stmt.order_by(document_nodes.c.path)
        async with self._engine.connect() as conn:
            return [_to_domain(row) for row in await conn.execute(stmt)]

    async def get_subtree(self, node_id: UUID) -> list[DocumentNode]:
        """A node and everything beneath it, via the ltree containment operator.

        `<@` is why `path` is an ltree with a GiST index: this is one index scan
        whatever the depth, where a parent_id walk would be one query per level.
        """
        stmt = sa.text(
            "SELECT c.* FROM core.document_nodes c "
            "JOIN core.document_nodes n ON n.id = :node_id "
            "WHERE c.document_id = n.document_id AND c.path <@ n.path "
            "ORDER BY c.path"
        )
        async with self._engine.connect() as conn:
            rows = await conn.execute(stmt, {"node_id": node_id})
            return [_to_domain(row) for row in rows]

    async def get_ancestors(self, node_id: UUID) -> list[DocumentNode]:
        """The chain from root down to *node_id*, inclusive.

        This is what turns a search hit into a citation a person can check:
        the titles along this chain are "Vol. II, Pt. 3, ch. 14".
        """
        stmt = sa.text(
            "SELECT a.* FROM core.document_nodes a "
            "JOIN core.document_nodes n ON n.id = :node_id "
            "WHERE a.document_id = n.document_id AND n.path <@ a.path "
            "ORDER BY a.path"
        )
        async with self._engine.connect() as conn:
            rows = await conn.execute(stmt, {"node_id": node_id})
            return [_to_domain(row) for row in rows]

    async def find_by_span(
        self, document_id: UUID, char_start: int, char_end: int
    ) -> DocumentNode | None:
        """The deepest node whose span encloses ``[char_start, char_end)``.

        Deepest, not first: every span is enclosed by the root, and the useful
        answer is the most specific one — the section a passage sits in, not the
        document it belongs to.
        """
        stmt = (
            document_nodes.select()
            .where(
                document_nodes.c.document_id == document_id,
                document_nodes.c.char_start <= char_start,
                document_nodes.c.char_end >= char_end,
            )
            .order_by(document_nodes.c.depth.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return _to_domain(row) if row is not None else None

    async def delete_for_document(self, tx: Transaction, document_id: UUID) -> int:
        """Drop a document's whole tree. Re-parsing replaces it wholesale."""
        result = await tx.conn.execute(
            document_nodes.delete().where(document_nodes.c.document_id == document_id)
        )
        return result.rowcount or 0

    async def count(self) -> int:
        async with self._engine.connect() as conn:
            return (
                await conn.execute(
                    sa.select(sa.func.count()).select_from(document_nodes)
                )
            ).scalar_one()
