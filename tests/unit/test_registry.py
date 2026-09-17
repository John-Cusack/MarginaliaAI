"""Tests for plugin registry."""

from __future__ import annotations

import pytest

from research_engine.domain.errors import PluginConflict, PluginLoadError, UnknownType
from research_engine.plugins.registry import PluginRegistry


def test_decorator_metadata_wins_over_manifest_blurb():
    """Precedence WI-4 decided: the decorator is richer, the manifest fills gaps."""
    from research_engine_sdk import tool

    @tool(
        id="x",
        description="rich decorator description",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    async def handler(**kwargs):
        return {}

    r = PluginRegistry()
    r.register_mcp_tool(
        "x", handler, "p",
        description="one-line blurb", input_schema={"type": "object"},
    )
    spec = r.get_mcp_tool_specs()["x"]
    assert spec.description == "rich decorator description"
    assert spec.input_schema["properties"] == {"q": {"type": "string"}}


def test_manifest_fills_gap_for_bare_entrypoint():
    async def handler(**kwargs):
        return {}

    r = PluginRegistry()
    r.register_mcp_tool(
        "x", handler, "p",
        description="manifest blurb", input_schema={"type": "object"},
    )
    spec = r.get_mcp_tool_specs()["x"]
    assert spec.description == "manifest blurb"
    assert spec.input_schema == {"type": "object"}




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

    def test_two_packs_cannot_own_the_same_document_type(self):
        """A document type decides which chunker runs, so one owner only."""
        r = PluginRegistry()
        r.register_document_type("letter", {}, "history")
        with pytest.raises(PluginConflict):
            r.register_document_type("letter", {}, "other_plugin")

    def test_two_packs_cannot_own_the_same_tool_id(self):
        r = PluginRegistry()
        r.register_mcp_tool("find_things", lambda: None, "history")
        with pytest.raises(PluginConflict):
            r.register_mcp_tool("find_things", lambda: None, "other_plugin")

    def test_same_plugin_no_conflict(self):
        r = PluginRegistry()
        r.register_document_type("letter", {}, "history")
        # Same plugin re-registering should not conflict
        r.register_document_type("letter", {}, "history")

    def test_two_packs_may_declare_the_same_entity_type(self):
        """Vocabulary is shared, not owned.

        `person` is declared by core and by every domain pack that has people in
        it. Treating that as a conflict made the whole pack fail to load — which
        is why the history pack, and with it the `letter` document type, its
        schemas and its tools, was unreachable.
        """
        r = PluginRegistry()
        r.register_core_types()

        r.register_entity_type("person", {}, "history")

        assert "person" in r.list_entity_types()
        assert r.list_entity_types()["person"]["plugin"] == "core"

    @pytest.mark.parametrize(
        ("register", "lister"),
        [
            ("register_entity_type", "list_entity_types"),
            ("register_event_type", "list_event_types"),
            ("register_relation_type", "list_relation_types"),
        ],
    )
    def test_the_first_declaration_of_a_term_keeps_its_definition(
        self, register: str, lister: str
    ):
        """A later pack must not redefine a type the first one is already using."""
        r = PluginRegistry()
        getattr(r, register)("battle", {"description": "armed engagement"}, "history")

        getattr(r, register)("battle", {"description": "something else"}, "other_plugin")

        stored = getattr(r, lister)()["battle"]
        assert stored["description"] == "armed engagement"
        assert stored["plugin"] == "history"

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

    def test_extraction_schemas_can_be_read_back(self):
        """Registering without a reader left pack schemas unrunnable.

        The loader parses a pack's declared schemas into the registry, and the
        executor resolves schemas from the database — with no way to enumerate
        what a pack registered, nothing could carry them across, so a pack could
        ship an extraction schema that could never execute.
        """
        r = PluginRegistry()
        definition = {"id": "scripture_claims", "record_types": []}

        r.register_extraction_schema("scripture_claims", 2, definition, "logos")

        assert r.get_extraction_schemas() == [
            ("scripture_claims", 2, definition, "logos")
        ]

    def test_no_extraction_schemas(self):
        assert PluginRegistry().get_extraction_schemas() == []


def test_discarded_stage_leaves_every_live_contribution_unchanged() -> None:
    registry = PluginRegistry()
    registry.register_core_types()
    stage = registry.create_stage()
    stage.register_document_type("letter", {}, "history")
    stage.register_vocabulary("calendar", {"months": []}, "history")
    stage.register_extraction_schema("claims", 1, {"record_types": []}, "history")
    stage.register_mcp_tool(
        "history.fail",
        lambda: None,
        "history",
        input_schema={"type": "object"},
    )

    stage.discard()

    assert registry.list_document_types() == {}
    assert registry.get_vocabularies() == {}
    assert registry.get_extraction_schemas() == []
    assert registry.get_mcp_tools() == {}


def test_committed_stage_publishes_complete_snapshot() -> None:
    registry = PluginRegistry()
    registry.register_core_types()
    stage = registry.create_stage()
    stage.register_document_type("letter", {}, "history")
    stage.register_mcp_tool(
        "history.run",
        lambda: None,
        "history",
        input_schema={"type": "object"},
    )

    stage.commit()

    assert set(registry.list_document_types()) == {"letter"}
    assert set(registry.get_mcp_tools()) == {"history.run"}


def test_stale_stage_cannot_overwrite_another_commit() -> None:
    registry = PluginRegistry()
    first = registry.create_stage()
    second = registry.create_stage()
    first.register_document_type("letter", {}, "history")
    second.register_document_type("article", {}, "journal")

    first.commit()

    with pytest.raises(PluginLoadError, match="changed while contributions were staged"):
        second.commit()
    assert set(registry.list_document_types()) == {"letter"}
