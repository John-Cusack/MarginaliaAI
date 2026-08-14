"""Postgres installed plugin repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from research_engine.adapters.storage.postgres.schema import installed_packs
from research_engine.domain.provenance import InstalledPlugin

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class PGInstalledPluginRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, plugin: InstalledPlugin) -> None:
        values = {
            "id": plugin.id,
            "version": plugin.version,
            "source_url": plugin.source_url,
            "source_ref": plugin.source_ref,
            "installed_at": plugin.installed_at,
            "enabled": plugin.enabled,
            "manifest": plugin.manifest,
            "permissions_granted": plugin.permissions_granted,
        }
        async with self._engine.begin() as conn:
            await conn.execute(installed_packs.insert().values(**values))

    async def get(self, plugin_id: str) -> InstalledPlugin | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    installed_packs.select().where(installed_packs.c.id == plugin_id)
                )
            ).first()
            return self._to_domain(row) if row else None

    async def list_enabled(self) -> list[InstalledPlugin]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    installed_packs.select().where(installed_packs.c.enabled == True)  # noqa: E712
                )
            ).all()
            return [self._to_domain(row) for row in rows]

    async def list_all(self) -> list[InstalledPlugin]:
        async with self._engine.connect() as conn:
            rows = (await conn.execute(installed_packs.select())).all()
            return [self._to_domain(row) for row in rows]

    async def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                installed_packs.update()
                .where(installed_packs.c.id == plugin_id)
                .values(enabled=enabled)
            )

    async def delete(self, plugin_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                installed_packs.delete().where(installed_packs.c.id == plugin_id)
            )

    @staticmethod
    def _to_domain(row: Any) -> InstalledPlugin:
        return InstalledPlugin(
            id=row.id,
            version=row.version,
            source_url=row.source_url,
            source_ref=row.source_ref,
            installed_at=row.installed_at,
            enabled=row.enabled,
            manifest=row.manifest or {},
            permissions_granted=row.permissions_granted or {},
        )
