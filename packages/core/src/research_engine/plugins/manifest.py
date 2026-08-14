"""Pydantic models for pack.yaml manifest parsing."""

from __future__ import annotations

import enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class NetworkPerm(enum.StrEnum):
    none = "none"
    egress = "egress"
    full = "full"


class FilesystemPerm(enum.StrEnum):
    default = "default"
    read_corpus = "read_corpus"
    read_write_plugin_data = "read_write_plugin_data"


class PluginDep(BaseModel):
    name: str
    version: str


class PluginCompatibility(BaseModel):
    core_api: str = ">=0.1.0,<1.0.0"
    python: str = ">=3.11"
    plugins: list[PluginDep] = Field(default_factory=list)
    pip: list[str] = Field(default_factory=list)
    setup_commands: list[str] = Field(default_factory=list)


class PluginPermissions(BaseModel):
    network: NetworkPerm = NetworkPerm.none
    network_allowlist: list[str] = Field(default_factory=list)
    llm: bool = False
    ingest: bool = False
    # Write access to the derived graph (edges). Gates the EdgeClient so a
    # plugin can record relations (e.g. citation `cites` edges) in the core
    # corpus graph.
    write: bool = False
    filesystem: FilesystemPerm = FilesystemPerm.default
    subprocess: bool = False


class DocumentTypeContribution(BaseModel):
    id: str
    schema_path: Path | None = Field(None, alias="schema")
    default_chunker: str = "prose_window"
    default_ingestion_module: str | None = None
    post_hooks: list[str] = Field(default_factory=list)


class EntityTypeContribution(BaseModel):
    id: str
    schema_path: Path | None = Field(None, alias="schema")


class EventTypeContribution(BaseModel):
    id: str
    schema_path: Path | None = Field(None, alias="schema")


class RelationTypeContribution(BaseModel):
    id: str
    inverse: str | None = None


class IngestionModuleContribution(BaseModel):
    id: str
    entry: str  # "module.path:ClassName"


class ChunkerContribution(BaseModel):
    id: str
    entry: str


class ExtractionSchemaContribution(BaseModel):
    id: str
    version: int
    file: Path


class MCPToolContribution(BaseModel):
    id: str
    entry: str
    description: str


class HookContribution(BaseModel):
    id: str
    entry: str
    event: str = "post_ingestion"


class VocabularyContribution(BaseModel):
    id: str
    file: Path


class FilterExtensionContribution(BaseModel):
    id: str
    entry: str  # "module.path:ClassName"
    description: str


class SourceSearchContribution(BaseModel):
    """A SourceSearchProvider contribution. ``id`` is informational only —
    the provider's ``plugin_name`` is the registry key (one provider per plugin)."""

    id: str
    entry: str  # "module.path:ClassName"
    description: str


class PluginContributions(BaseModel):
    document_types: list[DocumentTypeContribution] = Field(default_factory=list)
    entity_types: list[EntityTypeContribution] = Field(default_factory=list)
    event_types: list[EventTypeContribution] = Field(default_factory=list)
    relation_types: list[RelationTypeContribution] = Field(default_factory=list)
    ingestion_modules: list[IngestionModuleContribution] = Field(default_factory=list)
    chunkers: list[ChunkerContribution] = Field(default_factory=list)
    extraction_schemas: list[ExtractionSchemaContribution] = Field(default_factory=list)
    mcp_tools: list[MCPToolContribution] = Field(default_factory=list)
    post_ingestion_hooks: list[HookContribution] = Field(default_factory=list)
    vocabularies: list[VocabularyContribution] = Field(default_factory=list)
    filter_extensions: list[FilterExtensionContribution] = Field(default_factory=list)
    source_search: list[SourceSearchContribution] = Field(default_factory=list)


class PluginManifest(BaseModel):
    name: str
    version: str
    author: str
    description: str
    license: str = "MIT"
    homepage: str | None = None
    requires: PluginCompatibility = Field(default_factory=PluginCompatibility)
    permissions: PluginPermissions = Field(default_factory=PluginPermissions)
    provides: PluginContributions = Field(default_factory=PluginContributions)

    model_config = {"populate_by_name": True}


def parse_manifest(path: Path) -> PluginManifest:
    """Parse a pack.yaml file into a PluginManifest."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return PluginManifest(**data)
