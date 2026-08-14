"""Adapter that conforms the core edge repository to the SDK ``EdgeClient``
Protocol.

Plugins with the ``write`` permission receive an instance of this adapter as
their ``edge`` client, letting them record relations (e.g. citation ``cites``
edges) in the core corpus graph without touching the domain layer. ``create``
dedups on the natural key via ``PGEdgeRepo.upsert``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from research_engine.domain.common import NodeKind
from research_engine.domain.edges import EdgeDraft

if TYPE_CHECKING:
    from research_engine.adapters.storage.postgres.repositories.edges import PGEdgeRepo


class EdgeServiceAdapter:
    """Concrete implementation of the SDK ``EdgeClient`` Protocol."""

    def __init__(self, edge_repo: PGEdgeRepo, transaction_factory: Any) -> None:
        self._edges = edge_repo
        self._tx_factory = transaction_factory

    async def create(self, edge: dict) -> dict:
        """Insert-or-update a directed edge from a flat dict.

        Required keys: ``source_kind``, ``source_id``, ``target_kind``,
        ``target_id``, ``relation_type``. Optional: ``attributes``,
        ``source_passage_id``, ``confidence``.
        """
        source_passage_id = edge.get("source_passage_id")
        draft = EdgeDraft(
            source_kind=NodeKind(edge["source_kind"]),
            source_id=UUID(str(edge["source_id"])),
            target_kind=NodeKind(edge["target_kind"]),
            target_id=UUID(str(edge["target_id"])),
            relation_type=edge["relation_type"],
            attributes=edge.get("attributes") or {},
            source_passage_id=UUID(str(source_passage_id)) if source_passage_id else None,
            confidence=edge.get("confidence", 1.0),
        )
        async with self._tx_factory() as tx:
            created = await self._edges.upsert(tx, draft)
        return self._to_dict(created)

    async def query(
        self,
        *,
        source_id: UUID | None = None,
        target_id: UUID | None = None,
        relation_type: str | None = None,
    ) -> list[dict]:
        """Read edges by source or target node id (one is required)."""
        if source_id is not None:
            edges = await self._edges.query_by_source(
                NodeKind.document.value, UUID(str(source_id)), relation_type
            )
        elif target_id is not None:
            edges = await self._edges.query_by_target(
                NodeKind.document.value, UUID(str(target_id)), relation_type
            )
        else:
            raise ValueError("query requires either source_id or target_id")
        return [self._to_dict(e) for e in edges]

    @staticmethod
    def _to_dict(edge: Any) -> dict:
        return {
            "id": str(edge.id),
            "source_kind": edge.source_kind.value,
            "source_id": str(edge.source_id),
            "target_kind": edge.target_kind.value,
            "target_id": str(edge.target_id),
            "relation_type": edge.relation_type,
            "attributes": edge.attributes,
            "source_passage_id": str(edge.source_passage_id) if edge.source_passage_id else None,
            "confidence": edge.confidence,
        }
