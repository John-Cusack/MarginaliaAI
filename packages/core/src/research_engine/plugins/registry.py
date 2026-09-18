"""Plugin registry — in-memory type catalog for all contributions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from research_engine.domain.errors import PluginConflict, PluginLoadError, UnknownType

if TYPE_CHECKING:
    from collections.abc import Callable

    from research_engine.domain.filter_extension import FilterExtension
    from research_engine_sdk import SourceSearchProvider

logger = structlog.get_logger()

# Singleton reference set by the composition root, used by pipeline.py
# to resolve plugin-contributed chunkers without import-time coupling.
_global_registry: PluginRegistry | None = None


@dataclass(frozen=True)
class ToolSpec:
    """One pack tool as the agent sees it: handler plus its metadata.

    The description and schema live here — one record — rather than as
    attributes stamped onto the imported function, so the loader (which holds
    the manifest) and dispatch (which lists the tools) cannot disagree.
    """

    id: str
    handler: Any
    description: str
    input_schema: dict[str, Any]
    plugin: str



class PluginRegistryStage:
    """Isolated registry mutation that replaces the live snapshot on commit."""

    def __init__(self, live: PluginRegistry) -> None:
        self._live = live
        self._base_revision = live._revision
        self.registry = live._clone()
        self._closed = False

    def commit(self) -> None:
        if self._closed:
            raise PluginLoadError("plugin registry stage is already closed")
        if self._live._revision != self._base_revision:
            raise PluginLoadError(
                "plugin registry changed while contributions were staged; "
                "discard and rebuild the stage"
            )
        self._live._replace_from(self.registry)
        self._closed = True

    def discard(self) -> None:
        self._closed = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)

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
        self._mcp_tools: dict[str, ToolSpec] = {}
        self._post_ingestion_hooks: dict[str, list[Callable]] = {}
        self._filter_extensions: dict[str, FilterExtension] = {}
        self._source_search_providers: dict[str, SourceSearchProvider] = {}
        self._vocabularies: dict[str, Any] = {}
        self._revision = 0
        self._owners: dict[tuple[str, str], str] = {}

    def _touch(self) -> None:
        self._revision += 1

    def _clone(self) -> PluginRegistry:
        clone = PluginRegistry()
        clone._document_types = dict(self._document_types)
        clone._entity_types = dict(self._entity_types)
        clone._event_types = dict(self._event_types)
        clone._relation_types = dict(self._relation_types)
        clone._ingestion_modules = dict(self._ingestion_modules)
        clone._chunkers = dict(self._chunkers)
        clone._extraction_schemas = dict(self._extraction_schemas)
        clone._mcp_tools = dict(self._mcp_tools)
        clone._post_ingestion_hooks = {
            key: list(value) for key, value in self._post_ingestion_hooks.items()
        }
        clone._filter_extensions = dict(self._filter_extensions)
        clone._source_search_providers = dict(self._source_search_providers)
        clone._vocabularies = dict(self._vocabularies)
        clone._owners = dict(self._owners)
        clone._revision = self._revision
        return clone

    def _replace_from(self, staged: PluginRegistry) -> None:
        replacement = staged._clone()
        self._document_types = replacement._document_types
        self._entity_types = replacement._entity_types
        self._event_types = replacement._event_types
        self._relation_types = replacement._relation_types
        self._ingestion_modules = replacement._ingestion_modules
        self._chunkers = replacement._chunkers
        self._extraction_schemas = replacement._extraction_schemas
        self._mcp_tools = replacement._mcp_tools
        self._post_ingestion_hooks = replacement._post_ingestion_hooks
        self._filter_extensions = replacement._filter_extensions
        self._source_search_providers = replacement._source_search_providers
        self._vocabularies = replacement._vocabularies
        self._owners = replacement._owners
        self._revision = max(self._revision, replacement._revision) + 1

    def create_stage(self) -> PluginRegistryStage:
        return PluginRegistryStage(self)

    def _check_conflict(self, kind: str, id: str, plugin_name: str) -> None:
        """Claim an id that exactly one provider may own.

        Used for the contributions where a second implementation would have to
        displace the first: a document type decides which chunker runs, an MCP
        tool id decides which handler answers. Two claimants is a real conflict
        and the pack should fail to load.
        """
        key = (kind, id)
        if key in self._owners and self._owners[key] != plugin_name:
            raise PluginConflict(id, self._owners[key], plugin_name)
        self._owners[key] = plugin_name

    def _claim_vocabulary(self, kind: str, id: str, plugin_name: str) -> bool:
        """Register a shared vocabulary term. True if this is its first declaration.

        Entity, event and relation types are vocabulary, not resources. `person`
        is declared by core and by every domain pack that has people in it, and
        that is agreement rather than contention — nothing has to be displaced,
        because the type is only a name both parties use.

        Treating it as a conflict meant a pack declaring one common type failed
        to load *entirely*: the history pack, whose `person` collided with
        core's, so its letter document type, its schemas and its tools were all
        unreachable — the whole reason the Civil War volumes were never typed as
        correspondence.

        The first declaration keeps ownership and every attribute it defines.
        A later declaration may complete attributes the owner left undefined,
        but a genuine disagreement is logged and cannot replace the owner's
        value.
        """
        key = (kind, id)
        owner = self._owners.get(key)
        if owner is None:
            self._owners[key] = plugin_name
            return True
        if owner != plugin_name:
            logger.debug(
                "vocabulary_shared", kind=kind, id=id, owner=owner, also=plugin_name
            )
        return False

    def _merge_vocabulary_completion(
        self,
        kind: str,
        id: str,
        plugin: str,
        existing: dict[str, Any],
        spec: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return a completed copy, while keeping defined owner values immutable."""
        supplied = {
            key: value
            for key, value in spec.items()
            if key != "plugin" and value is not None
        }
        differing = {
            key: (existing[key], value)
            for key, value in supplied.items()
            if key in existing and existing[key] != value
        }
        if differing:
            logger.warning(
                "vocabulary_redefined_ignored",
                kind=kind,
                id=id,
                plugin=plugin,
                owner=existing.get("plugin"),
                differing=sorted(differing),
            )

        completion = {
            key: value for key, value in supplied.items() if key not in existing
        }
        if not completion:
            return None
        # Stages shallow-copy their registries. Never mutate the nested live
        # dictionary before the stage commits.
        return {**existing, **completion}

    # --- Document types ---

    def register_document_type(self, id: str, spec: dict[str, Any], plugin: str) -> None:
        self._check_conflict("document_type", id, plugin)
        self._document_types[id] = {**spec, "plugin": plugin}
        self._touch()

    def validate_document_type(self, doc_type: str) -> None:
        if doc_type != "generic" and doc_type not in self._document_types:
            raise UnknownType("document_type", doc_type, hint="Is the providing plugin enabled?")

    def list_document_types(self) -> dict[str, dict[str, Any]]:
        return dict(self._document_types)

    # --- Entity types ---

    def register_entity_type(self, id: str, spec: dict[str, Any], plugin: str) -> None:
        if not self._claim_vocabulary("entity_type", id, plugin):
            merged = self._merge_vocabulary_completion(
                "entity_type", id, plugin, self._entity_types.get(id, {}), spec
            )
            if merged is not None:
                self._entity_types[id] = merged
                self._touch()
            return
        self._entity_types[id] = {**spec, "plugin": plugin}
        self._touch()

    def validate_entity_type(self, entity_type: str) -> None:
        if entity_type not in self._entity_types:
            raise UnknownType("entity_type", entity_type, hint="Is the providing plugin enabled?")

    def list_entity_types(self) -> dict[str, dict[str, Any]]:
        return dict(self._entity_types)

    # --- Event types ---

    def register_event_type(self, id: str, spec: dict[str, Any], plugin: str) -> None:
        if not self._claim_vocabulary("event_type", id, plugin):
            merged = self._merge_vocabulary_completion(
                "event_type", id, plugin, self._event_types.get(id, {}), spec
            )
            if merged is not None:
                self._event_types[id] = merged
                self._touch()
            return
        self._event_types[id] = {**spec, "plugin": plugin}
        self._touch()

    def list_event_types(self) -> dict[str, dict[str, Any]]:
        return dict(self._event_types)

    # --- Relation types ---

    def register_relation_type(self, id: str, spec: dict[str, Any], plugin: str) -> None:
        if not self._claim_vocabulary("relation_type", id, plugin):
            merged = self._merge_vocabulary_completion(
                "relation_type", id, plugin, self._relation_types.get(id, {}), spec
            )
            if merged is not None:
                self._relation_types[id] = merged
                self._touch()
            return
        self._relation_types[id] = {**spec, "plugin": plugin}
        self._touch()

    def list_relation_types(self) -> dict[str, dict[str, Any]]:
        return dict(self._relation_types)

    # --- Ingestion modules ---

    def register_ingestion_module(self, id: str, factory: Any, plugin: str) -> None:
        self._check_conflict("ingestion_module", id, plugin)
        self._ingestion_modules[id] = factory
        self._touch()

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
        self._touch()

    def resolve_chunker(self, id: str) -> Any:
        try:
            return self._chunkers[id]
        except KeyError as err:
            raise UnknownType("chunker", id) from err

    # --- Extraction schemas ---

    def register_extraction_schema(self, id: str, version: int, schema: Any, plugin: str) -> None:
        key = (id, version)
        owner_key = f"{id}:{version}"
        self._check_conflict("extraction_schema", owner_key, plugin)
        self._extraction_schemas[key] = schema
        self._touch()

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

    def register_mcp_tool(
        self,
        id: str,
        handler: Any,
        plugin: str,
        *,
        description: str | None = None,
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self._check_conflict("mcp_tool", id, plugin)
        # The decorator wins where a pack used it — the working packs carry
        # richer decorator metadata than their one-line manifest blurbs. The
        # manifest is the fallback, so a bare entrypoint is still described.
        self._mcp_tools[id] = ToolSpec(
            id=id,
            handler=handler,
            plugin=plugin,
            description=getattr(handler, "_tool_description", None)
            or description
            or id,
            input_schema=getattr(handler, "_tool_input_schema", None)
            or input_schema
            or {},
        )
        self._touch()

    def get_mcp_tools(self) -> dict[str, Any]:
        return {k: s.handler for k, s in self._mcp_tools.items()}

    def get_mcp_tool_specs(self) -> dict[str, ToolSpec]:
        return dict(self._mcp_tools)

    def get_tool_plugin(self, tool_id: str) -> str | None:
        """Return the plugin name that owns a given MCP tool."""
        return self._owners.get(("mcp_tool", tool_id))

    # --- Hooks ---

    def register_post_ingestion_hook(self, doc_type: str, hook: Callable, plugin: str) -> None:
        self._post_ingestion_hooks.setdefault(doc_type, []).append(hook)
        self._owners[("hook", f"{doc_type}:{plugin}")] = plugin
        self._touch()

    def get_post_ingestion_hooks(self, doc_type: str) -> list[Callable]:
        return self._post_ingestion_hooks.get(doc_type, [])

    # --- Filter extensions ---

    def register_filter_extension(self, id: str, ext: FilterExtension, plugin: str) -> None:
        self._check_conflict("filter_extension", id, plugin)
        self._filter_extensions[id] = ext
        self._touch()

    def get_filter_extensions(self) -> dict[str, FilterExtension]:
        return dict(self._filter_extensions)

    # --- Source search providers ---

    def register_source_search_provider(
        self, provider: SourceSearchProvider, plugin: str
    ) -> None:
        name = provider.plugin_name
        self._check_conflict("source_search_provider", name, plugin)
        self._source_search_providers[name] = provider
        self._touch()

    def get_source_search_providers(self) -> dict[str, SourceSearchProvider]:
        return dict(self._source_search_providers)

    # --- Packaged vocabularies ---

    def register_vocabulary(self, id: str, value: Any, plugin: str) -> None:
        self._check_conflict("vocabulary", id, plugin)
        self._vocabularies[id] = value
        self._touch()

    def get_vocabularies(self) -> dict[str, Any]:
        return dict(self._vocabularies)

    # --- Core types bootstrap ---

    def register_core_types(self) -> None:
        """Register built-in core types."""
        for et in ["person", "place", "organization", "concept"]:
            self._entity_types[et] = {"plugin": "core"}
            self._owners[("entity_type", et)] = "core"

        for rt in ["replies_to", "cites", "references", "part_of", "influenced_by"]:
            self._relation_types[rt] = {"plugin": "core"}
            self._owners[("relation_type", rt)] = "core"
        self._touch()
