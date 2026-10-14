"""Reader profiles, the roles they give editions, and the runs made under them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.dialects.postgresql import insert as pg_insert
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import (
    editions,
    profiles,
    runs,
    source_roles,
)
from research_engine.domain.profiles import Profile, Run, RunStatus, StoredRole

if TYPE_CHECKING:
    from datetime import date
    from decimal import Decimal
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.domain.profiles import RoleDraft
    from research_engine.ports.repositories import Transaction


class PGProfileRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert(
        self,
        tx: Transaction,
        *,
        key: str,
        version: int,
        profile_hash: str,
        title: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Profile:
        """Create the profile, or record that new data for it was loaded."""
        values = {
            "version": version,
            "profile_hash": profile_hash,
            "title": title,
            "settings": settings or {},
        }
        stmt = pg_insert(profiles).values(id=uuid7(), key=key, **values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[profiles.c.key],
            set_={**values, "loaded_at": sa.func.now()},
        ).returning(profiles)
        return Profile.model_validate((await tx.conn.execute(stmt)).one()._mapping)

    async def replace_roles(
        self,
        tx: Transaction,
        profile_id: UUID,
        roles: list[tuple[UUID, RoleDraft]],
    ) -> int:
        """Make the profile's roles exactly ``roles``: ``(edition_id, draft)`` pairs.

        A profile is loaded whole, so its roles are replaced whole: a role the
        data no longer declares must not linger. Two roles of one kind whose
        dates overlap for the same edition and scope are refused by the
        exclusion constraint — a contradiction in the data, for its author.
        """
        await tx.conn.execute(
            source_roles.delete().where(source_roles.c.profile_id == profile_id)
        )
        if roles:
            await tx.conn.execute(
                source_roles.insert(),
                [
                    {
                        "id": uuid7(),
                        "profile_id": profile_id,
                        "edition_id": edition_id,
                        "role": draft.role.value,
                        "tier": draft.tier,
                        "validity": Range(draft.valid_from, draft.valid_to, bounds="[)"),
                        "scope": draft.scope,
                        "notes": draft.notes,
                    }
                    for edition_id, draft in roles
                ],
            )
        return len(roles)

    async def get_by_key(self, key: str) -> Profile | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(profiles.select().where(profiles.c.key == key))
            ).first()
        return Profile.model_validate(row._mapping) if row else None

    async def roles(
        self,
        profile_id: UUID,
        *,
        on: date | None = None,
        edition_id: UUID | None = None,
    ) -> list[StoredRole]:
        """The profile's roles — all of them, or those in force on one date."""
        query = (
            sa.select(source_roles, editions.c.edition_key)
            .join(editions, editions.c.id == source_roles.c.edition_id)
            .where(source_roles.c.profile_id == profile_id)
            .order_by(
                source_roles.c.tier.nulls_last(),
                editions.c.edition_key,
                source_roles.c.role,
                sa.func.lower(source_roles.c.validity),
            )
        )
        if on is not None:
            query = query.where(source_roles.c.validity.contains(on))
        if edition_id is not None:
            query = query.where(source_roles.c.edition_id == edition_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [_role(row) for row in rows]


class PGRunRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def start(
        self,
        tx: Transaction,
        *,
        profile_id: UUID,
        stage: str,
        profile_hash: str,
        config: dict[str, Any],
        config_hash: str,
        code_version: str | None = None,
        model_versions: dict[str, Any] | None = None,
        items_in: int | None = None,
        cost_estimate: Decimal | None = None,
    ) -> Run:
        row = (
            await tx.conn.execute(
                runs.insert()
                .values(
                    id=uuid7(),
                    profile_id=profile_id,
                    stage=stage,
                    profile_hash=profile_hash,
                    config=config,
                    config_hash=config_hash,
                    code_version=code_version,
                    model_versions=model_versions or {},
                    items_in=items_in,
                    cost_estimate=cost_estimate,
                )
                .returning(runs)
            )
        ).one()
        return Run.model_validate(row._mapping)

    async def finish(
        self,
        tx: Transaction,
        run_id: UUID,
        *,
        status: RunStatus,
        items_out: int | None = None,
        stats: dict[str, Any] | None = None,
    ) -> Run:
        """Close a running run. A run already closed is not reopened or rewritten."""
        if status is RunStatus.running:
            raise ValueError("finish a run as finished or failed, not running")
        values: dict[str, Any] = {"status": status.value, "finished_at": sa.func.now()}
        for column, value in (
            ("items_out", items_out),
            ("stats", stats),
        ):
            if value is not None:
                values[column] = value
        row = (
            await tx.conn.execute(
                runs.update()
                .where(runs.c.id == run_id, runs.c.status == RunStatus.running.value)
                .values(**values)
                .returning(runs)
            )
        ).first()
        if row is None:
            raise ValueError(f"run {run_id} does not exist or is already closed")
        return Run.model_validate(row._mapping)

    async def get(self, run_id: UUID) -> Run | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(runs.select().where(runs.c.id == run_id))).first()
        return Run.model_validate(row._mapping) if row else None

    async def list(self, profile_id: UUID, *, stage: str | None = None) -> list[Run]:
        query = runs.select().where(runs.c.profile_id == profile_id)
        if stage is not None:
            query = query.where(runs.c.stage == stage)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query.order_by(runs.c.started_at))).all()
        return [Run.model_validate(row._mapping) for row in rows]


def _role(row: Any) -> StoredRole:
    validity = row.validity
    return StoredRole(
        id=row.id,
        profile_id=row.profile_id,
        edition_id=row.edition_id,
        edition_key=row.edition_key,
        role=row.role,
        tier=row.tier,
        valid_from=validity.lower,
        valid_to=validity.upper,
        scope=row.scope,
        notes=row.notes,
    )
