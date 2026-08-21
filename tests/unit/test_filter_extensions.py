"""Tests for the filter extension system."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from research_engine.domain.filter_extension import FilterExtension
from research_engine.domain.passages import SearchFilters
from research_engine.plugins.manifest import (
    FilterExtensionContribution,
    PluginContributions,
    parse_manifest,
)
from research_engine.plugins.registry import PluginRegistry
from research_engine.services.search.filter_extensions import (
    EventDateRangeFilter,
    HasExtractionFilter,
)

# ---------- Helpers ----------


class StubFilterExtension:
    """Minimal FilterExtension for testing."""

    def __init__(self, fid: str = "stub_filter") -> None:
        self._id = fid

    @property
    def filter_id(self) -> str:
        return self._id

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"value": {"type": "string"}}}

    @property
    def description(self) -> str:
        return "A stub filter for testing."

    def build_clause(self, value: Any) -> sa.sql.expression.SelectBase:
        # Return a trivial SELECT — not connected to a real table
        return sa.select(sa.literal_column("'00000000-0000-0000-0000-000000000000'::uuid").label("passage_id"))


# ---------- Protocol conformance ----------


class TestFilterExtensionProtocol:
    def test_stub_is_filter_extension(self):
        ext = StubFilterExtension()
        assert isinstance(ext, FilterExtension)

    def test_event_date_range_is_filter_extension(self):
        ext = EventDateRangeFilter()
        assert isinstance(ext, FilterExtension)

    def test_has_extraction_is_filter_extension(self):
        ext = HasExtractionFilter()
        assert isinstance(ext, FilterExtension)


# ---------- Registry ----------


class TestRegistryFilterExtensions:
    def test_register_and_retrieve(self):
        r = PluginRegistry()
        ext = StubFilterExtension()
        r.register_filter_extension("stub_filter", ext, "test_plugin")
        exts = r.get_filter_extensions()
        assert "stub_filter" in exts
        assert exts["stub_filter"] is ext

    def test_conflict_detection(self):
        from research_engine.domain.errors import PluginConflict

        r = PluginRegistry()
        ext1 = StubFilterExtension()
        ext2 = StubFilterExtension()
        r.register_filter_extension("my_filter", ext1, "plugin_a")
        with pytest.raises(PluginConflict):
            r.register_filter_extension("my_filter", ext2, "plugin_b")

    def test_same_plugin_no_conflict(self):
        r = PluginRegistry()
        ext = StubFilterExtension()
        r.register_filter_extension("my_filter", ext, "plugin_a")
        r.register_filter_extension("my_filter", ext, "plugin_a")

    def test_multiple_extensions(self):
        r = PluginRegistry()
        ext1 = StubFilterExtension("filter_a")
        ext2 = StubFilterExtension("filter_b")
        r.register_filter_extension("filter_a", ext1, "plugin_a")
        r.register_filter_extension("filter_b", ext2, "plugin_b")
        exts = r.get_filter_extensions()
        assert len(exts) == 2

    def test_empty_by_default(self):
        r = PluginRegistry()
        assert r.get_filter_extensions() == {}


# ---------- Manifest ----------


class TestManifestFilterExtensions:
    def test_filter_extension_contribution_model(self):
        c = FilterExtensionContribution(
            id="scripture_ref_range",
            entry="logos.filters:ScriptureRefRangeFilter",
            description="Filter by scripture reference range.",
        )
        assert c.id == "scripture_ref_range"
        assert c.entry == "logos.filters:ScriptureRefRangeFilter"

    def test_contributions_include_filter_extensions(self):
        c = PluginContributions(
            filter_extensions=[
                FilterExtensionContribution(
                    id="my_filter",
                    entry="mod:Cls",
                    description="test",
                ),
            ],
        )
        assert len(c.filter_extensions) == 1

    def test_parse_yaml_with_filter_extensions(self, tmp_path):
        yaml_content = """
name: test-plugin
version: 0.1.0
author: Test
description: Test plugin

provides:
  filter_extensions:
    - id: my_filter
      entry: "mod:MyFilter"
      description: "A custom filter."
"""
        manifest_path = tmp_path / "pack.yaml"
        manifest_path.write_text(yaml_content)

        m = parse_manifest(manifest_path)
        assert len(m.provides.filter_extensions) == 1
        assert m.provides.filter_extensions[0].id == "my_filter"
        assert m.provides.filter_extensions[0].entry == "mod:MyFilter"

    def test_parse_yaml_without_filter_extensions(self, tmp_path):
        yaml_content = """
name: test-plugin
version: 0.1.0
author: Test
description: Test plugin
"""
        manifest_path = tmp_path / "pack.yaml"
        manifest_path.write_text(yaml_content)

        m = parse_manifest(manifest_path)
        assert m.provides.filter_extensions == []


# ---------- SearchFilters ----------


class TestSearchFiltersExtensions:
    def test_extensions_field_default(self):
        sf = SearchFilters()
        assert sf.extensions is None
        assert sf.extension_logic == "and"

    def test_extensions_field_set(self):
        sf = SearchFilters(
            extensions={"event_date_range": {"start": "2020-01-01"}},
            extension_logic="or",
        )
        assert sf.extensions == {"event_date_range": {"start": "2020-01-01"}}
        assert sf.extension_logic == "or"

    def test_model_dump_excludes_none(self):
        sf = SearchFilters(document_types=["letter"])
        d = sf.model_dump(exclude_none=True)
        assert "extensions" not in d
        assert "document_types" in d

    def test_model_dump_includes_extensions_when_set(self):
        sf = SearchFilters(
            extensions={"my_filter": {"key": "val"}},
        )
        d = sf.model_dump(exclude_none=True)
        assert "extensions" in d
        assert d["extension_logic"] == "and"


# ---------- Built-in filters: schema validation ----------


class TestEventDateRangeFilter:
    def test_properties(self):
        f = EventDateRangeFilter()
        assert f.filter_id == "event_date_range"
        assert "start" in f.input_schema["properties"]
        assert f.input_schema["required"] == ["start"]
        assert len(f.description) > 0

    def test_build_clause_returns_select(self):
        f = EventDateRangeFilter()
        clause = f.build_clause({"start": "1805-01-01", "end": "1806-12-31"})
        assert isinstance(clause, sa.sql.expression.SelectBase)

    def test_build_clause_with_event_type(self):
        f = EventDateRangeFilter()
        clause = f.build_clause({
            "start": "1805-01-01",
            "event_type": "battle",
        })
        compiled = str(clause.compile(compile_kwargs={"literal_binds": True}))
        assert "event_type" in compiled

    def test_build_clause_with_actor_ids(self):
        f = EventDateRangeFilter()
        clause = f.build_clause({
            "start": "1805-01-01",
            "actor_entity_ids": ["00000000-0000-0000-0000-000000000001"],
        })
        compiled = str(clause.compile())
        assert "event_actors" in compiled


class TestHasExtractionFilter:
    def test_properties(self):
        f = HasExtractionFilter()
        assert f.filter_id == "has_extraction"
        assert "record_type" in f.input_schema["properties"]
        assert f.input_schema["required"] == ["record_type"]

    def test_build_clause_returns_select(self):
        f = HasExtractionFilter()
        clause = f.build_clause({"record_type": "citation"})
        assert isinstance(clause, sa.sql.expression.SelectBase)

    def test_build_clause_with_data_contains(self):
        f = HasExtractionFilter()
        clause = f.build_clause({
            "record_type": "citation",
            "data_contains": {"doi": "10.1234/test"},
        })
        compiled = str(clause.compile())
        assert "extraction_records" in compiled


# ---------- Dynamic schema ----------


class TestDynamicSchema:
    def test_no_extensions_returns_base_schema(self):
        from research_engine.mcp.tools.find_passages import TOOL_SCHEMA, build_dynamic_schema

        r = PluginRegistry()
        schema = build_dynamic_schema(r)
        # With no extensions, should return base schema unchanged
        assert schema is TOOL_SCHEMA

    def test_extensions_injected_into_schema(self):
        from research_engine.mcp.tools.find_passages import build_dynamic_schema

        r = PluginRegistry()
        ext = StubFilterExtension("my_ext")
        r.register_filter_extension("my_ext", ext, "test")

        schema = build_dynamic_schema(r)
        ext_props = schema["properties"]["filters"]["properties"]["extensions"]["properties"]
        assert "my_ext" in ext_props
        assert ext_props["my_ext"]["description"] == ext.description

    def test_dynamic_schema_does_not_mutate_base(self):
        from research_engine.mcp.tools.find_passages import TOOL_SCHEMA, build_dynamic_schema

        r = PluginRegistry()
        ext = StubFilterExtension("mutate_test")
        r.register_filter_extension("mutate_test", ext, "test")

        _dynamic = build_dynamic_schema(r)
        # Base schema should not have been mutated
        base_ext = TOOL_SCHEMA["properties"]["filters"]["properties"].get("extensions", {})
        assert "mutate_test" not in base_ext.get("properties", {})

    def test_multiple_extensions_in_schema(self):
        from research_engine.mcp.tools.find_passages import build_dynamic_schema

        r = PluginRegistry()
        r.register_filter_extension("ext_a", StubFilterExtension("ext_a"), "p1")
        r.register_filter_extension("ext_b", StubFilterExtension("ext_b"), "p2")

        schema = build_dynamic_schema(r)
        ext_props = schema["properties"]["filters"]["properties"]["extensions"]["properties"]
        assert "ext_a" in ext_props
        assert "ext_b" in ext_props


# ---------- list_available_filters tool ----------


class TestListFiltersHandler:
    @pytest.mark.asyncio
    async def test_returns_core_and_extensions(self):
        from research_engine.mcp.tools.list_filters import handler

        class FakeContainer:
            registry = PluginRegistry()

        container = FakeContainer()
        container.registry.register_core_types()
        ext = StubFilterExtension("test_ext")
        container.registry.register_filter_extension("test_ext", ext, "test")

        result = await handler(container)
        assert "core_filters" in result
        assert "extensions" in result
        assert len(result["extensions"]) == 1
        assert result["extensions"][0]["id"] == "test_ext"
        assert "available_entity_types" in result
        assert "person" in result["available_entity_types"]
