"""PostgreSQL persistence for plugin activation and approval state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.dialects.postgresql import insert

from research_engine.adapters.storage.postgres.schema import plugin_activations
from research_engine.domain.provenance import (
    PluginActivation,
    PluginActivationState,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class PGPluginActivationRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    def _values(activation: PluginActivation) -> dict[str, Any]:
        return activation.model_dump(mode="python")

    async def save(self, activation: PluginActivation) -> None:
        values = self._values(activation)
        statement = insert(plugin_activations).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[plugin_activations.c.plugin_id],
            set_={
                key: value
                for key, value in values.items()
                if key != "plugin_id"
            },
        )
        async with self._engine.begin() as conn:
            await conn.execute(statement)

    async def get(self, plugin_id: str) -> PluginActivation | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    plugin_activations.select().where(
                        plugin_activations.c.plugin_id == plugin_id
                    )
                )
            ).first()
        return self._to_domain(row) if row else None

    async def list_enabled(self) -> list[PluginActivation]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    plugin_activations.select()
                    .where(plugin_activations.c.enabled.is_(True))
                    .order_by(plugin_activations.c.plugin_id)
                )
            ).all()
        return [self._to_domain(row) for row in rows]

    async def list_all(self) -> list[PluginActivation]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    plugin_activations.select().order_by(
                        plugin_activations.c.plugin_id
                    )
                )
            ).all()
        return [self._to_domain(row) for row in rows]

    async def update_state(
        self,
        plugin_id: str,
        state: PluginActivationState,
        *,
        enabled: bool | None = None,
        last_error: str | None = None,
        last_seen_at: Any | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "state": state.value,
            "last_error": last_error,
        }
        if enabled is not None:
            values["enabled"] = enabled
        if last_seen_at is not None:
            values["last_seen_at"] = last_seen_at
        async with self._engine.begin() as conn:
            await conn.execute(
                plugin_activations.update()
                .where(plugin_activations.c.plugin_id == plugin_id)
                .values(**values)
            )

    async def record_migration(
        self,
        plugin_id: str,
        *,
        revision: int,
        status: str,
        state: PluginActivationState,
        last_error: str | None = None,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                plugin_activations.update()
                .where(plugin_activations.c.plugin_id == plugin_id)
                .values(
                    database_revision=revision,
                    database_status=status,
                    state=state.value,
                    last_error=last_error,
                )
            )

    async def delete(self, plugin_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                plugin_activations.delete().where(
                    plugin_activations.c.plugin_id == plugin_id
                )
            )

    @staticmethod
    def _to_domain(row: Any) -> PluginActivation:
        return PluginActivation.model_validate(dict(row._mapping))
