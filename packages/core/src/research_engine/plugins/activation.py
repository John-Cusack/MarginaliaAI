"""Plugin discovery/approval state machine, independent of CLI presentation."""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from research_engine.domain.provenance import (
    PluginActivation,
    PluginActivationState,
)
from research_engine.plugins.discovery import (
    DiscoveredPlugin,
    DiscoveryIssue,
    scan_plugins,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from research_engine.ports.repositories import PluginActivationRepo


class PluginNotInstalledError(LookupError):
    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        super().__init__(f"Plugin {plugin_id!r} is not installed in this Python environment")


class PluginMigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PluginStatus:
    plugin_id: str
    state: PluginActivationState
    discovered: DiscoveredPlugin | None = None
    activation: PluginActivation | None = None
    reason: str | None = None


class PluginActivationManager:
    def __init__(self, repository: PluginActivationRepo) -> None:
        self._repository = repository

    async def inventory(
        self,
        *,
        persist: bool = False,
        discovered_plugins: Iterable[DiscoveredPlugin] | None = None,
        issues: Iterable[DiscoveryIssue] | None = None,
    ) -> list[PluginStatus]:
        if discovered_plugins is None:
            report = scan_plugins()
            discovered = list(report.plugins)
            discovered_issues = list(report.issues)
        else:
            discovered = list(discovered_plugins)
            discovered_issues = list(issues or ())

        activations = {
            activation.plugin_id: activation
            for activation in await self._repository.list_all()
        }
        by_id = {plugin.plugin_id: plugin for plugin in discovered}
        statuses: dict[str, PluginStatus] = {}
        now = datetime.now(UTC)

        for plugin_id, plugin in by_id.items():
            activation = activations.get(plugin_id)
            if activation is None:
                statuses[plugin_id] = PluginStatus(
                    plugin_id,
                    PluginActivationState.available,
                    discovered=plugin,
                )
                continue
            if activation.state is PluginActivationState.legacy:
                state = PluginActivationState.legacy
                reason = "legacy source-install row requires fresh distribution approval"
            elif not self._matches(activation, plugin):
                state = PluginActivationState.pending_approval
                reason = "installed distribution version or manifest differs from approval"
            elif (
                plugin.manifest.provides.database is not None
                and (activation.database_revision or 0)
                < plugin.manifest.provides.database.current_revision
            ):
                state = PluginActivationState.error
                reason = (
                    "migration required: approved database revision "
                    f"{activation.database_revision or 0}, declared revision "
                    f"{plugin.manifest.provides.database.current_revision}"
                )
            elif activation.enabled:
                state = PluginActivationState.enabled
                reason = None
            else:
                state = PluginActivationState.disabled
                reason = None
            statuses[plugin_id] = PluginStatus(
                plugin_id,
                state,
                discovered=plugin,
                activation=activation,
                reason=reason,
            )
            if persist and activation.state is not PluginActivationState.legacy:
                await self._repository.update_state(
                    plugin_id,
                    state,
                    enabled=activation.enabled,
                    last_error=reason,
                    last_seen_at=now,
                )

        for plugin_id, activation in activations.items():
            if plugin_id in statuses:
                continue
            if activation.state is PluginActivationState.legacy:
                state = PluginActivationState.legacy
                reason = "legacy source-install row; executable directory is not loaded"
            else:
                state = PluginActivationState.missing
                reason = "approved distribution is not installed"
                if persist:
                    await self._repository.update_state(
                        plugin_id,
                        state,
                        enabled=activation.enabled,
                        last_error=reason,
                    )
            statuses[plugin_id] = PluginStatus(
                plugin_id,
                state,
                activation=activation,
                reason=reason,
            )

        for issue in discovered_issues:
            plugin_id = issue.entry_point_name or issue.distribution_name
            state = (
                PluginActivationState.incompatible
                if issue.reason.startswith("requires core_api")
                or issue.reason.startswith("requires Python")
                else PluginActivationState.error
            )
            statuses.setdefault(
                plugin_id,
                PluginStatus(plugin_id, state, reason=issue.reason),
            )

        return [statuses[key] for key in sorted(statuses)]

    async def audit(
        self,
        plugin_id: str,
        *,
        discovered_plugins: Iterable[DiscoveredPlugin] | None = None,
    ) -> PluginStatus:
        statuses = await self.inventory(discovered_plugins=discovered_plugins)
        for status in statuses:
            if status.plugin_id == plugin_id:
                return status
        raise PluginNotInstalledError(plugin_id)

    async def approve(
        self,
        plugin_id: str,
        *,
        non_interactive: bool,
        discovered_plugins: Iterable[DiscoveredPlugin] | None = None,
    ) -> PluginActivation:
        plugin = self._find_discovered(plugin_id, discovered_plugins)
        previous = await self._repository.get(plugin_id)
        now = datetime.now(UTC)
        activation = PluginActivation(
            plugin_id=plugin.plugin_id,
            distribution_name=plugin.distribution_name,
            distribution_version=plugin.distribution_version,
            entry_point_name=plugin.entry_point_name,
            manifest_sha256=plugin.manifest_sha256,
            manifest=plugin.manifest.model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
            permissions_granted=plugin.manifest.permissions.model_dump(mode="json"),
            installed_at=previous.installed_at if previous is not None else now,
            enabled=True,
            state=PluginActivationState.enabled,
            approved_at=now,
            approved_non_interactive=non_interactive,
            last_seen_at=now,
            last_error=None,
            provenance={
                "direct_url": plugin.direct_url,
                "project_urls": plugin.project_urls,
            },
            legacy_source_url=(
                previous.legacy_source_url if previous is not None else None
            ),
            legacy_source_ref=(
                previous.legacy_source_ref if previous is not None else None
            ),
            database_revision=(
                previous.database_revision if previous is not None else None
            ),
            database_status=(
                previous.database_status if previous is not None else None
            ),
        )
        await self._repository.save(activation)
        return activation

    async def migrate(
        self,
        plugin_id: str,
        *,
        database_url: str,
        data_root: Path,
        discovered_plugins: Iterable[DiscoveredPlugin] | None = None,
    ) -> dict[str, Any]:
        plugin = self._find_discovered(plugin_id, discovered_plugins)
        activation = await self._repository.get(plugin_id)
        if (
            activation is None
            or activation.state is PluginActivationState.legacy
            or activation.approved_at is None
            or not self._matches(activation, plugin)
        ):
            raise PluginMigrationError(
                f"plugin {plugin_id!r} must have an exact approval before migration"
            )
        declaration = plugin.manifest.provides.database
        if declaration is None:
            raise PluginMigrationError(
                f"plugin {plugin_id!r} declares no database migration"
            )

        context = self._plugin_context(plugin, data_root)
        try:
            status_entry = self._load_approved_entry(
                plugin, declaration.status_entry
            )
            before_raw = await self._call_migration_entry(
                status_entry,
                context=context,
                database_url=database_url,
            )
            before_revision, before_status = self._migration_status(
                before_raw,
                fallback_revision=activation.database_revision or 0,
            )
            if before_revision < declaration.current_revision:
                upgrade_entry = self._load_approved_entry(
                    plugin, declaration.upgrade_entry
                )
                await self._call_migration_entry(
                    upgrade_entry,
                    context=context,
                    database_url=database_url,
                )
            after_raw = await self._call_migration_entry(
                status_entry,
                context=context,
                database_url=database_url,
            )
            revision, status = self._migration_status(
                after_raw,
                fallback_revision=before_revision,
            )
            if revision < declaration.current_revision:
                raise PluginMigrationError(
                    f"plugin {plugin_id!r} migration reported revision {revision}; "
                    f"manifest requires {declaration.current_revision}"
                )
            state = (
                PluginActivationState.enabled
                if activation.enabled
                else PluginActivationState.disabled
            )
            await self._repository.record_migration(
                plugin_id,
                revision=revision,
                status=status,
                state=state,
                last_error=None,
            )
            return {
                "plugin_id": plugin_id,
                "previous_revision": before_revision,
                "current_revision": revision,
                "status": status,
                "previous_status": before_status,
            }
        except Exception as exc:
            await self._repository.record_migration(
                plugin_id,
                revision=activation.database_revision or 0,
                status="error",
                state=PluginActivationState.error,
                last_error=str(exc) or type(exc).__name__,
            )
            if isinstance(exc, PluginMigrationError):
                raise
            raise PluginMigrationError(
                f"plugin {plugin_id!r} migration failed: "
                f"{str(exc) or type(exc).__name__}"
            ) from exc

    @staticmethod
    def _plugin_context(
        plugin: DiscoveredPlugin,
        data_root: Path,
    ):
        from research_engine_sdk import PluginContext

        data_dir = (data_root / plugin.plugin_id).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        return PluginContext(
            plugin_id=plugin.plugin_id,
            data_dir=data_dir,
            distribution_name=plugin.distribution_name,
            distribution_version=plugin.distribution_version,
        )

    @staticmethod
    def _load_approved_entry(plugin: DiscoveredPlugin, entry: str):
        module_name, attribute = entry.rsplit(":", 1)
        if module_name != plugin.module_name and not module_name.startswith(
            f"{plugin.module_name}."
        ):
            raise PluginMigrationError(
                f"migration entry {entry!r} is outside approved package "
                f"{plugin.module_name!r}"
            )
        module = importlib.import_module(module_name)
        try:
            return getattr(module, attribute)
        except AttributeError as exc:
            raise PluginMigrationError(
                f"approved migration entry {entry!r} does not exist"
            ) from exc

    @staticmethod
    async def _call_migration_entry(entry, **kwargs):
        result = entry(**kwargs)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _migration_status(
        value: Any,
        *,
        fallback_revision: int,
    ) -> tuple[int, str]:
        if isinstance(value, int):
            return value, "ok"
        if isinstance(value, dict):
            revision = value.get("current_revision", value.get("revision"))
            status = str(value.get("status", "ok"))
            return (
                int(revision) if revision is not None else fallback_revision,
                status,
            )
        if value is None:
            return fallback_revision, "unknown"
        raise PluginMigrationError(
            "migration status entry must return an integer, mapping, or None"
        )

    async def disable(self, plugin_id: str) -> None:
        activation = await self._repository.get(plugin_id)
        if activation is None:
            raise PluginNotInstalledError(plugin_id)
        await self._repository.update_state(
            plugin_id,
            PluginActivationState.disabled,
            enabled=False,
            last_error=None,
        )

    async def forget(self, plugin_id: str) -> None:
        if await self._repository.get(plugin_id) is None:
            raise PluginNotInstalledError(plugin_id)
        await self._repository.delete(plugin_id)

    @staticmethod
    def _matches(
        activation: PluginActivation,
        plugin: DiscoveredPlugin,
    ) -> bool:
        return (
            activation.distribution_name == plugin.distribution_name
            and activation.distribution_version == plugin.distribution_version
            and activation.entry_point_name == plugin.entry_point_name
            and activation.manifest_sha256 == plugin.manifest_sha256
        )

    @staticmethod
    def _find_discovered(
        plugin_id: str,
        discovered_plugins: Iterable[DiscoveredPlugin] | None,
    ) -> DiscoveredPlugin:
        plugins = (
            list(discovered_plugins)
            if discovered_plugins is not None
            else list(scan_plugins().plugins)
        )
        for plugin in plugins:
            if plugin.plugin_id == plugin_id:
                return plugin
        raise PluginNotInstalledError(plugin_id)
