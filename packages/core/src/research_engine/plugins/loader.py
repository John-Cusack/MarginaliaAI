"""Approved plugin loading from installed Python distributions."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from research_engine.domain.errors import PluginLoadError
from research_engine.plugins.discovery import DiscoveredPlugin, scan_plugins
from research_engine.plugins.permissions import (
    DeniedEdgeClient,
    DeniedHttpClient,
    DeniedIngestionClient,
    DeniedLLMClient,
    GatedHttpClient,
)
from research_engine_sdk import PluginContext

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from research_engine.plugins.registry import PluginRegistry
    from research_engine.ports.repositories import PluginActivationRepo

logger = structlog.get_logger()


@dataclass(slots=True)
class LoadedPlugin:
    discovery: DiscoveredPlugin
    tools: dict[str, Any] = field(default_factory=dict)

    @property
    def manifest(self):
        return self.discovery.manifest


class PluginLoader:
    def __init__(
        self,
        plugin_activations: PluginActivationRepo,
        registry: PluginRegistry,
        plugin_data_dir: Path,
        llm: Any = None,
        http: Any = None,
        database_url: str | None = None,
        **services: Any,
    ) -> None:
        self._activations = plugin_activations
        self._registry = registry
        self._plugin_data_dir = plugin_data_dir
        self._llm = llm
        self._http = http
        self._database_url = database_url
        self._services = services
        self._loaded: dict[str, LoadedPlugin] = {}
        self.on_tools_changed: Callable[[], None] | None = None

    @staticmethod
    def _activation_id(activation: Any) -> str:
        return str(
            getattr(activation, "plugin_id", None)
            or getattr(activation, "id", "")
        )

    @staticmethod
    def _is_exactly_approved(
        activation: Any, discovered: DiscoveredPlugin
    ) -> bool:
        database = discovered.manifest.provides.database
        migration_ready = (
            database is None
            or (getattr(activation, "database_revision", None) or 0)
            >= database.current_revision
        )
        return bool(
            getattr(activation, "enabled", False)
            and getattr(activation, "state", "legacy") == "enabled"
            and getattr(activation, "distribution_name", None)
            == discovered.distribution_name
            and getattr(activation, "distribution_version", None)
            == discovered.distribution_version
            and getattr(activation, "entry_point_name", None)
            == discovered.entry_point_name
            and getattr(activation, "manifest_sha256", None)
            == discovered.manifest_sha256
            and migration_ready
        )

    @staticmethod
    def _resolve_plugin_dependencies(
        discovered: list[DiscoveredPlugin],
    ) -> list[DiscoveredPlugin]:
        surviving = list(discovered)
        while True:
            available = {
                plugin.plugin_id: plugin.distribution_version
                for plugin in surviving
            }
            kept: list[DiscoveredPlugin] = []
            for plugin in surviving:
                missing = False
                for dependency in plugin.manifest.requires.plugins:
                    installed = available.get(dependency.name)
                    if installed is None or Version(installed) not in SpecifierSet(
                        dependency.version
                    ):
                        logger.error(
                            "plugin_dependency_unsatisfied",
                            plugin=plugin.plugin_id,
                            dependency=dependency.name,
                            required=dependency.version,
                            installed=installed,
                        )
                        missing = True
                        break
                if not missing:
                    kept.append(plugin)
            if len(kept) == len(surviving):
                return kept
            surviving = kept

    async def load_enabled(
        self,
        discovered_plugins: Iterable[DiscoveredPlugin] | None = None,
    ) -> list[str]:
        """Load only exact, enabled approvals in deterministic plugin-id order."""

        if discovered_plugins is None:
            report = scan_plugins()
            for issue in report.issues:
                logger.error(
                    "plugin_discovery_rejected",
                    plugin=issue.entry_point_name,
                    distribution=issue.distribution_name,
                    reason=issue.reason,
                )
            discovered = list(report.plugins)
        else:
            discovered = sorted(discovered_plugins, key=lambda plugin: plugin.plugin_id)

        enabled = {
            self._activation_id(activation): activation
            for activation in await self._activations.list_enabled()
        }
        approved = [
            plugin
            for plugin in discovered
            if (activation := enabled.get(plugin.plugin_id)) is not None
            and self._is_exactly_approved(activation, plugin)
        ]
        approved = self._resolve_plugin_dependencies(approved)

        loaded_names: list[str] = []
        for plugin in approved:
            try:
                await self._load_one(plugin)
            except Exception as exc:
                logger.error(
                    "plugin_load_failed",
                    plugin=plugin.plugin_id,
                    distribution=plugin.distribution_name,
                    error=str(exc) or type(exc).__name__,
                )
                continue
            loaded_names.append(plugin.plugin_id)
            logger.info(
                "plugin_loaded",
                plugin=plugin.plugin_id,
                distribution=plugin.distribution_name,
                version=plugin.distribution_version,
            )
        return loaded_names

    async def _load_one(self, discovered: DiscoveredPlugin) -> None:
        """Import and validate every contribution in an isolated registry stage."""

        manifest = discovered.manifest
        plugin_id = manifest.plugin_id
        provides = manifest.provides
        stage = self._registry.create_stage()
        staged = stage.registry
        loaded = LoadedPlugin(discovery=discovered)

        try:
            for contribution in provides.document_types:
                staged.register_document_type(
                    contribution.id,
                    {
                        "default_chunker": contribution.default_chunker,
                        "default_ingestion_module": (
                            contribution.default_ingestion_module
                        ),
                        "schema": contribution.schema_path,
                    },
                    plugin_id,
                )
            for contribution in provides.entity_types:
                staged.register_entity_type(
                    contribution.id,
                    {"schema": contribution.schema_path},
                    plugin_id,
                )
            for contribution in provides.event_types:
                staged.register_event_type(
                    contribution.id,
                    {"schema": contribution.schema_path},
                    plugin_id,
                )
            for contribution in provides.relation_types:
                staged.register_relation_type(
                    contribution.id,
                    {"inverse": contribution.inverse},
                    plugin_id,
                )

            for contribution in provides.chunkers:
                staged.register_chunker(
                    contribution.id,
                    self._import_entry(discovered, contribution.entry),
                    plugin_id,
                )
            for contribution in provides.ingestion_modules:
                staged.register_ingestion_module(
                    contribution.id,
                    self._import_entry(discovered, contribution.entry),
                    plugin_id,
                )
            for contribution in provides.filter_extensions:
                value = self._import_entry(discovered, contribution.entry)
                staged.register_filter_extension(
                    contribution.id,
                    value() if isinstance(value, type) else value,
                    plugin_id,
                )
            for contribution in provides.source_search:
                value = self._import_entry(discovered, contribution.entry)
                staged.register_source_search_provider(
                    value() if isinstance(value, type) else value,
                    plugin_id,
                )

            for contribution in provides.mcp_tools:
                handler = self._import_entry(discovered, contribution.entry)
                loaded.tools[contribution.id] = handler
                staged.register_mcp_tool(
                    contribution.id,
                    handler,
                    plugin_id,
                    description=contribution.description,
                    input_schema=contribution.input_schema,
                )
                if not staged.get_mcp_tool_specs()[contribution.id].input_schema:
                    raise PluginLoadError(
                        f"tool {contribution.id!r} has no input schema"
                    )

            for contribution in provides.extraction_schemas:
                definition = yaml.safe_load(
                    discovered.resource_path(contribution.file).read_text()
                )
                if not isinstance(definition, dict):
                    raise PluginLoadError(
                        f"schema {contribution.id!r} must contain a YAML object"
                    )
                staged.register_extraction_schema(
                    contribution.id,
                    contribution.version,
                    definition,
                    plugin_id,
                )

            for contribution in provides.vocabularies:
                definition = yaml.safe_load(
                    discovered.resource_path(contribution.file).read_text()
                )
                staged.register_vocabulary(
                    contribution.id,
                    definition,
                    plugin_id,
                )

            hook_handlers: dict[str, Any] = {}
            for contribution in provides.post_ingestion_hooks:
                if contribution.event != "post_ingestion":
                    raise PluginLoadError(
                        f"unsupported hook event {contribution.event!r}"
                    )
                hook_handlers[contribution.id] = self._import_entry(
                    discovered, contribution.entry
                )
            for document_type in provides.document_types:
                for hook_id in document_type.post_hooks:
                    try:
                        handler = hook_handlers[hook_id]
                    except KeyError as exc:
                        raise PluginLoadError(
                            f"document type {document_type.id!r} references "
                            f"unknown hook {hook_id!r}"
                        ) from exc
                    staged.register_post_ingestion_hook(
                        document_type.id, handler, plugin_id
                    )
            for handler in hook_handlers.values():
                for document_type in getattr(handler, "_hook_document_types", ()):
                    staged.register_post_ingestion_hook(
                        document_type, handler, plugin_id
                    )

            stage.commit()
        except Exception as exc:
            stage.discard()
            if isinstance(exc, PluginLoadError):
                raise
            raise PluginLoadError(
                f"failed to load plugin {plugin_id!r}: "
                f"{str(exc) or type(exc).__name__}"
            ) from exc

        self._loaded[plugin_id] = loaded
        if self.on_tools_changed is not None:
            self.on_tools_changed()

    @staticmethod
    def _import_entry(discovered: DiscoveredPlugin, entry: str) -> Any:
        module_path, attribute = entry.rsplit(":", 1)
        if module_path != discovered.module_name and not module_path.startswith(
            f"{discovered.module_name}."
        ):
            raise PluginLoadError(
                f"entry {entry!r} is outside package {discovered.module_name!r}"
            )
        module = importlib.import_module(module_path)
        try:
            return getattr(module, attribute)
        except AttributeError as exc:
            raise PluginLoadError(f"entry {entry!r} does not exist") from exc

    def build_plugin_clients(self, plugin_name: str) -> dict[str, Any]:
        loaded = self._loaded.get(plugin_name)
        if loaded is None:
            return {}

        discovered = loaded.discovery
        permissions = discovered.manifest.permissions
        from research_engine.adapters.corpus_client import CorpusServiceAdapter

        search = self._services.get("search")
        documents = self._services.get("documents")
        passages = self._services.get("passages")
        corpus_client: Any
        if search is not None and documents is not None and passages is not None:
            corpus_client = CorpusServiceAdapter(
                search,
                documents,
                passages,
                self._services.get("document_nodes"),
            )
        else:
            corpus_client = search

        data_dir = (self._plugin_data_dir / plugin_name).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        clients = {
            "context": PluginContext(
                plugin_id=plugin_name,
                data_dir=data_dir,
                distribution_name=discovered.distribution_name,
                distribution_version=discovered.distribution_version,
                database_url=self._database_url,
            ),
            "corpus": corpus_client,
            "entity": self._services.get("entity"),
            "event": self._services.get("event"),
            "extraction": self._services.get("extraction"),
            "llm": (
                self._llm
                if permissions.llm and self._llm is not None
                else DeniedLLMClient(plugin_name)
            ),
            "http": (
                GatedHttpClient(self._http, permissions, plugin_name)
                if permissions.network.value != "none" and self._http is not None
                else DeniedHttpClient(plugin_name)
            ),
            "ingestion": (
                self._services["ingestion"]
                if permissions.ingest and self._services.get("ingestion") is not None
                else DeniedIngestionClient(plugin_name)
            ),
            "edge": (
                self._services["edge"]
                if permissions.write and self._services.get("edge") is not None
                else DeniedEdgeClient(plugin_name)
            ),
        }
        return clients

    @property
    def loaded_plugins(self) -> dict[str, LoadedPlugin]:
        return dict(self._loaded)
