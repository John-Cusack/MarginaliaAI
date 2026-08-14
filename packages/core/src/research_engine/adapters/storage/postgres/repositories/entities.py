"""Postgres entity repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import entities, entity_aliases
from research_engine.domain.entities import Entity, EntityAlias, EntityCandidate, EntityDraft

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction


class PGEntityRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, tx: Transaction, draft: EntityDraft) -> Entity:
        entity_id = uuid7()
        values = {
            "id": entity_id,
            "entity_type": draft.entity_type,
            "canonical_name": draft.canonical_name,
            "disambiguator": draft.disambiguator,
            "attributes": draft.attributes,
        }
        await tx.conn.execute(entities.insert().values(**values))
        return await self._get_by_id(tx.conn, entity_id)  # type: ignore[return-value]

    async def get(self, entity_id: UUID) -> Entity | None:
        async with self._engine.connect() as conn:
            return await self._get_by_id(conn, entity_id)

    async def update(self, entity_id: UUID, patch: dict[str, Any]) -> Entity:
        async with self._engine.begin() as conn:
            update_values: dict[str, Any] = {}
            if "canonical_name" in patch:
                update_values["canonical_name"] = patch["canonical_name"]
            if "disambiguator" in patch:
                update_values["disambiguator"] = patch["disambiguator"]
            if "entity_type" in patch:
                update_values["entity_type"] = patch["entity_type"]
            if "attributes" in patch:
                existing = (
                    await conn.execute(
                        sa.select(entities.c.attributes).where(entities.c.id == entity_id)
                    )
                ).scalar_one()
                update_values["attributes"] = {**(existing or {}), **patch["attributes"]}
            update_values["updated_at"] = sa.func.now()
            await conn.execute(
                entities.update().where(entities.c.id == entity_id).values(**update_values)
            )
            return await self._get_by_id(conn, entity_id)  # type: ignore[return-value]

    async def search_by_name(
        self, query: str, entity_type: str | None, k: int
    ) -> list[EntityCandidate]:
        stmt = (
            sa.select(
                entities,
                sa.func.similarity(entities.c.canonical_name, query).label("score"),
            )
            .where(sa.func.similarity(entities.c.canonical_name, query) > 0.1)
            .order_by(sa.desc("score"))
            .limit(k)
        )
        if entity_type is not None:
            stmt = stmt.where(entities.c.entity_type == entity_type)

        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
            candidates: list[EntityCandidate] = []
            for row in rows:
                # Fetch aliases for each candidate
                alias_rows = (
                    await conn.execute(
                        entity_aliases.select().where(
                            entity_aliases.c.entity_id == row.id
                        )
                    )
                ).all()
                candidates.append(
                    EntityCandidate(
                        entity_id=row.id,
                        canonical_name=row.canonical_name,
                        entity_type=row.entity_type,
                        disambiguator=row.disambiguator,
                        aliases=[r.alias for r in alias_rows],
                        attributes=row.attributes or {},
                        match_score=row.score,
                    )
                )
            return candidates

    async def get_aliases(self, entity_id: UUID) -> list[EntityAlias]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    entity_aliases.select().where(entity_aliases.c.entity_id == entity_id)
                )
            ).all()
            return [
                EntityAlias(
                    entity_id=row.entity_id,
                    alias=row.alias,
                    alias_type=row.alias_type,
                )
                for row in rows
            ]

    async def add_alias(self, tx: Transaction, alias: EntityAlias) -> None:
        await tx.conn.execute(
            entity_aliases.insert().values(
                entity_id=alias.entity_id,
                alias=alias.alias,
                alias_type=alias.alias_type,
            )
        )

    async def list_by_type(self, entity_type: str, limit: int = 100) -> list[Entity]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    entities.select()
                    .where(entities.c.entity_type == entity_type)
                    .limit(limit)
                )
            ).all()
            return [self._to_domain(row) for row in rows]

    async def count(self) -> int:
        async with self._engine.connect() as conn:
            return (await conn.execute(sa.select(sa.func.count()).select_from(entities))).scalar_one()

    async def _get_by_id(self, conn: Any, entity_id: UUID) -> Entity | None:
        row = (
            await conn.execute(entities.select().where(entities.c.id == entity_id))
        ).first()
        return self._to_domain(row) if row else None

    @staticmethod
    def _to_domain(row: Any) -> Entity:
        return Entity(
            id=row.id,
            entity_type=row.entity_type,
            canonical_name=row.canonical_name,
            disambiguator=row.disambiguator,
            attributes=row.attributes or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
