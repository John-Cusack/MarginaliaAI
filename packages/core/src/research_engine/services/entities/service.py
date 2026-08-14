"""Entity service with tiered resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from research_engine.domain.entities import Entity, EntityAlias, EntityCandidate, EntityDraft

if TYPE_CHECKING:
    from uuid import UUID

    from research_engine.ports.repositories import EntityRepo, MentionRepo, Transaction

logger = structlog.get_logger()


class EntityService:
    def __init__(self, entities: EntityRepo, mentions: MentionRepo) -> None:
        self._entities = entities
        self._mentions = mentions

    async def resolve(
        self,
        name: str,
        entity_type: str | None = None,
        k: int = 5,
    ) -> list[EntityCandidate]:
        """Tiered entity resolution: exact → alias → trigram fuzzy."""
        return await self._entities.search_by_name(name, entity_type, k)

    async def get(self, entity_id: UUID) -> Entity | None:
        return await self._entities.get(entity_id)

    async def get_with_aliases(self, entity_id: UUID) -> tuple[Entity | None, list[EntityAlias]]:
        entity = await self._entities.get(entity_id)
        if not entity:
            return None, []
        aliases = await self._entities.get_aliases(entity_id)
        return entity, aliases

    async def upsert(
        self,
        tx: Transaction,
        draft: EntityDraft,
        aliases: list[str] | None = None,
    ) -> Entity:
        """Create or update an entity."""
        # Try exact match first
        candidates = await self._entities.search_by_name(
            draft.canonical_name, draft.entity_type, k=1
        )
        if candidates and candidates[0].match_score > 0.95:
            # Update existing
            entity_id = candidates[0].entity_id
            entity = await self._entities.update(entity_id, draft.attributes)
            logger.info(
                "entity_updated",
                entity_id=str(entity_id),
                name=draft.canonical_name,
            )
        else:
            # Create new
            entity = await self._entities.insert(tx, draft)
            logger.info(
                "entity_created",
                entity_id=str(entity.id),
                name=draft.canonical_name,
            )

        # Add aliases
        if aliases:
            import contextlib

            for alias in aliases:
                with contextlib.suppress(Exception):
                    await self._entities.add_alias(
                        tx, EntityAlias(entity_id=entity.id, alias=alias)
                    )

        return entity

    async def find_mentions(
        self,
        entity_id: UUID,
        filters: dict[str, Any] | None = None,
        k: int = 100,
    ) -> list:
        return await self._mentions.get_by_entity(entity_id, filters, k)

    async def list_by_type(self, entity_type: str, limit: int = 100) -> list[Entity]:
        return await self._entities.list_by_type(entity_type, limit)
