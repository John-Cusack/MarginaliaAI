"""Validated static runtime manifest for Research Engine plugins."""

from __future__ import annotations

import enum
import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_ENTRY = re.compile(
    r"^(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):(?P<attribute>[A-Za-z_]\w*)$"
)


def _resource_path(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError("resource path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("resource path must be relative and may not traverse with '..'")
    if ":" in path.parts[0]:
        raise ValueError("resource path may not contain a drive prefix")
    return path.as_posix()


ResourcePath = Annotated[str, AfterValidator(_resource_path)]


def entry_module(entry: str) -> str:
    """Return the module portion of a validated ``module:attribute`` entry."""

    match = _ENTRY.fullmatch(entry)
    if match is None:
        raise ValueError(f"invalid entry {entry!r}; expected module.path:attribute")
    return match.group("module")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NetworkPermission(enum.StrEnum):
    none = "none"
    egress = "egress"
    full = "full"


class FilesystemPermission(enum.StrEnum):
    none = "none"
    plugin_data = "plugin_data"
    read_corpus = "read_corpus"
    read_write_plugin_data = "read_write_plugin_data"


# Source compatibility for plugins written against the pre-v2 enum names.
NetworkPerm = NetworkPermission
FilesystemPerm = FilesystemPermission


class PluginRequirement(_StrictModel):
    name: str
    version: str

    @field_validator("version")
    @classmethod
    def _valid_version_specifier(cls, value: str) -> str:
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError(f"invalid plugin version specifier {value!r}") from exc
        return value


class PluginCompatibility(_StrictModel):
    core_api: str
    python: str = ">=3.11"
    plugins: list[PluginRequirement] = Field(default_factory=list)

    @field_validator("core_api", "python")
    @classmethod
    def _valid_specifier(cls, value: str) -> str:
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError(f"invalid compatibility specifier {value!r}") from exc
        return value


class PluginPermissions(_StrictModel):
    network: NetworkPermission = NetworkPermission.none
    network_allowlist: list[str] = Field(default_factory=list)
    filesystem: FilesystemPermission = FilesystemPermission.none
    llm: bool = False
    ingest: bool = False
    write: bool = False
    subprocess: bool = False

    @model_validator(mode="after")
    def _allowlist_requires_network(self) -> PluginPermissions:
        if self.network is NetworkPermission.none and self.network_allowlist:
            raise ValueError("network_allowlist requires network permission")
        return self


class DocumentTypeContribution(_StrictModel):
    id: str
    schema_path: ResourcePath | None = Field(default=None, alias="schema")
    default_chunker: str = "prose_window"
    default_ingestion_module: str | None = None
    post_hooks: list[str] = Field(default_factory=list)


class EntityTypeContribution(_StrictModel):
    id: str
    schema_path: ResourcePath | None = Field(default=None, alias="schema")


class EventTypeContribution(_StrictModel):
    id: str
    schema_path: ResourcePath | None = Field(default=None, alias="schema")


class RelationTypeContribution(_StrictModel):
    id: str
    inverse: str | None = None


class IngestionModuleContribution(_StrictModel):
    id: str
    entry: str

    @field_validator("entry")
    @classmethod
    def _valid_entry(cls, value: str) -> str:
        entry_module(value)
        return value


class ChunkerContribution(_StrictModel):
    id: str
    entry: str

    @field_validator("entry")
    @classmethod
    def _valid_entry(cls, value: str) -> str:
        entry_module(value)
        return value


class ExtractionSchemaContribution(_StrictModel):
    id: str
    version: int = Field(ge=1)
    file: ResourcePath


class ToolContribution(_StrictModel):
    id: str
    entry: str
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]

    @field_validator("entry")
    @classmethod
    def _valid_entry(cls, value: str) -> str:
        entry_module(value)
        return value

    @field_validator("input_schema")
    @classmethod
    def _valid_input_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("tool input_schema must have type: object")
        properties = value.get("properties")
        if properties is not None and not isinstance(properties, dict):
            raise ValueError("tool input_schema properties must be an object")
        return value


MCPToolContribution = ToolContribution


class HookContribution(_StrictModel):
    id: str
    entry: str
    event: str = "post_ingestion"

    @field_validator("entry")
    @classmethod
    def _valid_entry(cls, value: str) -> str:
        entry_module(value)
        return value


class VocabularyContribution(_StrictModel):
    id: str
    file: ResourcePath


class FilterExtensionContribution(_StrictModel):
    id: str
    entry: str
    description: str = Field(min_length=1)

    @field_validator("entry")
    @classmethod
    def _valid_entry(cls, value: str) -> str:
        entry_module(value)
        return value


class SourceSearchContribution(_StrictModel):
    id: str
    entry: str
    description: str = Field(min_length=1)

    @field_validator("entry")
    @classmethod
    def _valid_entry(cls, value: str) -> str:
        entry_module(value)
        return value


class DatabaseContribution(_StrictModel):
    current_revision: int = Field(ge=0)
    upgrade_entry: str
    status_entry: str

    @field_validator("upgrade_entry", "status_entry")
    @classmethod
    def _valid_entry(cls, value: str) -> str:
        entry_module(value)
        return value


class PluginContributions(_StrictModel):
    document_types: list[DocumentTypeContribution] = Field(default_factory=list)
    entity_types: list[EntityTypeContribution] = Field(default_factory=list)
    event_types: list[EventTypeContribution] = Field(default_factory=list)
    relation_types: list[RelationTypeContribution] = Field(default_factory=list)
    ingestion_modules: list[IngestionModuleContribution] = Field(default_factory=list)
    chunkers: list[ChunkerContribution] = Field(default_factory=list)
    extraction_schemas: list[ExtractionSchemaContribution] = Field(default_factory=list)
    mcp_tools: list[ToolContribution] = Field(default_factory=list)
    post_ingestion_hooks: list[HookContribution] = Field(default_factory=list)
    vocabularies: list[VocabularyContribution] = Field(default_factory=list)
    filter_extensions: list[FilterExtensionContribution] = Field(default_factory=list)
    source_search: list[SourceSearchContribution] = Field(default_factory=list)
    database: DatabaseContribution | None = None


class PluginManifest(_StrictModel):
    schema_version: Literal[2]
    plugin_id: str
    requires: PluginCompatibility
    permissions: PluginPermissions = Field(default_factory=PluginPermissions)
    provides: PluginContributions = Field(default_factory=PluginContributions)

    @field_validator("plugin_id")
    @classmethod
    def _valid_plugin_id(cls, value: str) -> str:
        if _ID.fullmatch(value) is None:
            raise ValueError(
                "plugin_id must contain only lowercase letters, digits, '.', '_' or '-'"
            )
        return value

    @model_validator(mode="after")
    def _validate_contribution_ids(self) -> PluginManifest:
        for tool in self.provides.mcp_tools:
            if not tool.id.startswith(f"{self.plugin_id}."):
                raise ValueError(
                    f"tool id {tool.id!r} must be namespaced by {self.plugin_id!r}"
                )
        groups = {
            "document type": self.provides.document_types,
            "entity type": self.provides.entity_types,
            "event type": self.provides.event_types,
            "relation type": self.provides.relation_types,
            "ingestion module": self.provides.ingestion_modules,
            "chunker": self.provides.chunkers,
            "extraction schema": self.provides.extraction_schemas,
            "tool": self.provides.mcp_tools,
            "hook": self.provides.post_ingestion_hooks,
            "vocabulary": self.provides.vocabularies,
            "filter extension": self.provides.filter_extensions,
            "source search provider": self.provides.source_search,
        }
        for label, contributions in groups.items():
            ids = [item.id for item in contributions]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} id")
        return self

    def resource_paths(self) -> tuple[str, ...]:
        paths: list[str] = []
        for item in (
            *self.provides.document_types,
            *self.provides.entity_types,
            *self.provides.event_types,
        ):
            if item.schema_path is not None:
                paths.append(item.schema_path)
        paths.extend(item.file for item in self.provides.extraction_schemas)
        paths.extend(item.file for item in self.provides.vocabularies)
        return tuple(paths)

    def entry_values(self) -> tuple[str, ...]:
        entries = [
            item.entry
            for item in (
                *self.provides.ingestion_modules,
                *self.provides.chunkers,
                *self.provides.mcp_tools,
                *self.provides.post_ingestion_hooks,
                *self.provides.filter_extensions,
                *self.provides.source_search,
            )
        ]
        if self.provides.database is not None:
            entries.extend(
                [
                    self.provides.database.upgrade_entry,
                    self.provides.database.status_entry,
                ]
            )
        return tuple(entries)


def parse_manifest_data(data: Any) -> PluginManifest:
    if not isinstance(data, dict):
        raise ValueError("plugin manifest must contain a YAML object")
    return PluginManifest.model_validate(data)


def parse_manifest_bytes(data: bytes) -> PluginManifest:
    return parse_manifest_data(yaml.safe_load(data))


def parse_manifest(path: str | Path) -> PluginManifest:
    return parse_manifest_bytes(Path(path).read_bytes())
