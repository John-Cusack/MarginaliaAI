"""Tests for extraction schema parsing and prompt rendering."""

from __future__ import annotations

from research_engine.services.extraction.schemas import parse_schema_yaml, render_prompt


class TestParseSchemaYAML:
    def test_basic(self):
        yaml_str = """
id: test_schema
version: 1
description: Test
owner: core
record_types:
  - id: test_record
    fields:
      name:
        type: string
        required: true
prompt: "Extract from {{ passage_text }}"
"""
        result = parse_schema_yaml(yaml_str)
        assert result["id"] == "test_schema"
        assert result["version"] == 1
        assert len(result["record_types"]) == 1


class TestRenderPrompt:
    def test_basic(self):
        template = "Extract from: {{ passage_text }}"
        result = render_prompt(template, "Hello world")
        assert "Hello world" in result

    def test_entity_hints(self):
        template = "{{ passage_text }}\nHints: {{ entity_hints }}"
        result = render_prompt(template, "text", entity_hints="McClellan, Barlow")
        assert "McClellan, Barlow" in result

    def test_extra_context(self):
        template = "{{ passage_text }} - {{ custom_field }}"
        result = render_prompt(template, "text", extra_context={"custom_field": "extra"})
        assert "extra" in result
