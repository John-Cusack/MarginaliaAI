"""Tests for plugin registry."""

from __future__ import annotations

import pytest

from research_engine.domain.errors import PluginConflict, UnknownType
from research_engine.plugins.registry import PluginRegistry


class TestPluginRegistry:
    def test_register_core_types(self):
        r = PluginRegistry()
        r.register_core_types()
        assert "person" in r.list_entity_types()
        assert "replies_to" in r.list_relation_types()

    def test_register_document_type(self):
        r = PluginRegistry()
        r.register_document_type("letter", {"default_chunker": "whole_or_paragraph"}, "history")
        types = r.list_document_types()
        assert "letter" in types
        assert types["letter"]["plugin"] == "history"

    def test_validate_unknown_document_type(self):
        r = PluginRegistry()
        with pytest.raises(UnknownType):
            r.validate_document_type("nonexistent_type")

    def test_generic_always_valid(self):
        r = PluginRegistry()
        # Should not raise
        r.validate_document_type("generic")

    def test_conflict_detection(self):
        r = PluginRegistry()
        r.register_entity_type("battle", {}, "history")
        with pytest.raises(PluginConflict):
            r.register_entity_type("battle", {}, "other_plugin")

    def test_same_plugin_no_conflict(self):
        r = PluginRegistry()
        r.register_entity_type("battle", {}, "history")
        # Same plugin re-registering should not conflict
        r.register_entity_type("battle", {}, "history")

    def test_register_mcp_tool(self):
        r = PluginRegistry()

        async def handler(**kwargs):
            return {}

        r.register_mcp_tool("my_tool", handler, "test_plugin")
        tools = r.get_mcp_tools()
        assert "my_tool" in tools

    def test_post_ingestion_hooks(self):
        r = PluginRegistry()

        async def hook(doc, text, meta):
            pass

        r.register_post_ingestion_hook("letter", hook, "history")
        hooks = r.get_post_ingestion_hooks("letter")
        assert len(hooks) == 1
        assert hooks == r.get_post_ingestion_hooks("letter")

    def test_no_hooks_for_type(self):
        r = PluginRegistry()
        assert r.get_post_ingestion_hooks("nonexistent") == []
