"""Plugin registry — in-memory type catalog for all contributions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from research_engine.domain.errors import PluginConflict, UnknownType

if TYPE_CHECKING:
    from collections.abc import Callable

    from research_engine.domain.filter_extension import FilterExtension
    from research_engine.domain.source_search import SourceSearchProvider

logger = structlog.get_logger()

# Singleton reference set by the composition root, used by pipeline.py
# to resolve plugin-contributed chunkers without import-time coupling.
_global_registry: PluginRegistry | None = None


class PluginRegistry:
    """Central runtime catalog of all registered contributions."""

    def __init__(self) -> None:
        self._document_types: dict[str, dict[str, Any]] = {}
        self._entity_types: dict[str, dict[str, Any]] = {}
        self._event_types: dict[str, dict[str, Any]] = {}
        self._relation_types: dict[str, dict[str, Any]] = {}
        self._ingestion_modules: dict[str, Any] = {}
        self._chunkers: dict[str, Any] = {}
        self._extraction_schemas: dict[tuple[str, int], Any] = {}
        self._mcp_tools: dict[str, Any] = {}
        self._post_ingestion_hooks: dict[str, list[Callable]] = {}
        self._filter_extensions: dict[str, FilterExtension] = {}
        self._source_search_providers: dict[str, SourceSearchProvider] = {}
        self._owners: dict[tuple[str, str], str] = {}

    def _check_conflict(self, kind: str, id: str, plugin_name: str) -> None:
        key = (kind, id)
        if key in self._owners and self._owners[key] != plugin_name:
            raise PluginConflict(id, self._owners[key], plugin_name)
        self._owners[key] = plugin_name

    # --- Document types ---

    def register_document_type(self, id: str, spec: dict[str, Any], plugin: str) -> None:
        self._check_conflict("document_type", id, plugin)
        self._document_types[id] = {**spec, "plugin": plugin}

    def validate_document_type(self, doc_type: str) -> None:
        if doc_type != "generic" and doc_type not in self._document_types:
            raise UnknownType("document_type", doc_type, hint="Is the providing plugin enabled?")

    def list_document_types(self) -> dict[str, dict[str, Any]]:
        return dict(self._document_types)

    # --- Entity types ---

    def register_entity_type(self, id: str, spec: dict[str, Any], plugin: str) -> None:
        self._check_conflict("entity_type", id, plugin)
        self._entity_types[id] = {**spec, "plugin": plugin}

    def validate_entity_type(self, entity_type: str) -> None:
        if entity_type not in self._entity_types:
            raise UnknownType("entity_type", entity_type, hint="Is the providing plugin enabled?")

    def list_entity_types(self) -> dict[str, dict[str, Any]]:
        return dict(self._entity_types)

    # --- Event types ---

    def register_event_type(self, id: str, spec: dict[str, Any], plugin: str) -> None:
        self._check_conflict("event_type", id, plugin)
        self._event_types[id] = {**spec, "plugin": plugin}

    def list_event_types(self) -> dict[str, dict[str, Any]]:
        return dict(self._event_types)

    # --- Relation types ---

    def register_relation_type(self, id: str, spec: dict[str, Any], plugin: str) -> None:
        self._check_conflict("relation_type", id, plugin)
        self._relation_types[id] = {**spec, "plugin": plugin}

    def list_relation_types(self) -> dict[str, dict[str, Any]]:
        return dict(self._relation_types)

    # --- Ingestion modules ---

    def register_ingestion_module(self, id: str, factory: Any, plugin: str) -> None:
        self._check_conflict("ingestion_module", id, plugin)
        self._ingestion_modules[id] = factory

    def resolve_ingestion_module(self, id: str) -> Any:
        try:
            return self._ingestion_modules[id]
        except KeyError as err:
            raise UnknownType("ingestion_module", id, hint="Is the providing plugin enabled?") from err

    def iter_ingestion_modules(self) -> list[Any]:
        return list(self._ingestion_modules.values())

    # --- Chunkers ---

    def register_chunker(self, id: str, factory: Any, plugin: str) -> None:
        self._check_conflict("chunker", id, plugin)
        self._chunkers[id] = factory

    def resolve_chunker(self, id: str) -> Any:
        try:
            return self._chunkers[id]
        except KeyError as err:
            raise UnknownType("chunker", id) from err

    # --- Extraction schemas ---

    def register_extraction_schema(self, id: str, version: int, schema: Any, plugin: str) -> None:
        key = (id, version)
        self._extraction_schemas[key] = schema
        self._owners[("extraction_schema", f"{id}:{version}")] = plugin

    def get_extraction_schemas(self) -> list[tuple[str, int, Any, str]]:
        """Every pack-contributed schema, as ``(id, version, definition, owner)``.

        Registering without a way to read back left pack schemas in a dictionary
        nothing consulted: the executor resolves schemas from the database, so a
        pack could declare one and it could never run. `extraction sync` is what
        closes that gap, and this is what it reads.
        """
        return [
            (
                schema_id,
                version,
                schema,
                self._owners.get(("extraction_schema", f"{schema_id}:{version}"), ""),
            )
            for (schema_id, version), schema in self._extraction_schemas.items()
        ]

    # --- MCP tools ---

    def register_mcp_tool(self, id: str, handler: Any, plugin: str) -> None:
        self._check_conflict("mcp_tool", id, plugin)
        self._mcp_tools[id] = handler

    def get_mcp_tools(self) -> dict[str, Any]:
        return dict(self._mcp_tools)

    def get_tool_plugin(self, tool_id: str) -> str | None:
        """Return the plugin name that owns a given MCP tool."""
        return self._owners.get(("mcp_tool", tool_id))

    # --- Hooks ---

    def register_post_ingestion_hook(self, doc_type: str, hook: Callable, plugin: str) -> None:
        self._post_ingestion_hooks.setdefault(doc_type, []).append(hook)
        self._owners[("hook", f"{doc_type}:{plugin}")] = plugin

    def get_post_ingestion_hooks(self, doc_type: str) -> list[Callable]:
        return self._post_ingestion_hooks.get(doc_type, [])

    # --- Filter extensions ---

    def register_filter_extension(self, id: str, ext: FilterExtension, plugin: str) -> None:
        self._check_conflict("filter_extension", id, plugin)
        self._filter_extensions[id] = ext

    def get_filter_extensions(self) -> dict[str, FilterExtension]:
        return dict(self._filter_extensions)

    # --- Source search providers ---

    def register_source_search_provider(
        self, provider: SourceSearchProvider, plugin: str
    ) -> None:
        name = provider.plugin_name
        self._check_conflict("source_search_provider", name, plugin)
        self._source_search_providers[name] = provider

    def get_source_search_providers(self) -> dict[str, SourceSearchProvider]:
        return dict(self._source_search_providers)

    # --- Core types bootstrap ---

    def register_core_types(self) -> None:
        """Register built-in core types."""
        for et in ["person", "place", "organization", "concept"]:
            self._entity_types[et] = {"plugin": "core"}
            self._owners[("entity_type", et)] = "core"

        for rt in ["replies_to", "cites", "references", "part_of", "influenced_by"]:
            self._relation_types[rt] = {"plugin": "core"}
            self._owners[("relation_type", rt)] = "core"
