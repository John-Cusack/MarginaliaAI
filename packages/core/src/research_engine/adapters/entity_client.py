"""SDK entity client backed by the core entity service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from research_engine.domain.entities import EntityDraft

if TYPE_CHECKING:
    from research_engine.services.entities.service import EntityService


class EntityServiceAdapter:
    def __init__(self, service: EntityService, transaction_factory: Any) -> None:
        self._service = service
        self._transaction = transaction_factory

    async def upsert(self, entity: dict[str, Any]) -> dict[str, Any]:
        payload = dict(entity)
        aliases = payload.pop("aliases", None)
        async with self._transaction() as tx:
            stored = await self._service.upsert(
                tx,
                EntityDraft.model_validate(payload),
                aliases=aliases,
            )
        return stored.model_dump(mode="json")

    async def resolve(
        self, name: str, entity_type: str | None = None
    ) -> list[dict[str, Any]]:
        candidates = await self._service.resolve(name, entity_type)
        return [candidate.model_dump(mode="json") for candidate in candidates]
