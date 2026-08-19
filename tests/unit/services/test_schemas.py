"""Tests for extraction schema parsing and prompt rendering."""

from __future__ import annotations

import pytest

from research_engine.domain.errors import ValidationError
from research_engine.services.extraction.schemas import (
    build_output_schema,
    evidence_field_names,
    parse_schema_yaml,
    render_prompt,
)


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


class TestBuildOutputSchema:
    """The shape three things have to agree about.

    The JSON Schema sent to the provider, the validator, and
    `extraction_records` all describe the same records. When they disagreed, the
    generated schema nested record types as keys — so a record's fields arrived
    as a list under a type name, the validator's `isinstance(value, str)` test
    was false for every one of them, and evidence validation passed vacuously
    for every record ever extracted.
    """

    SCHEMA = {
        "record_types": [
            {
                "id": "claim",
                "fields": {
                    "assertion": {"type": "string", "required": True},
                    "quote": {"type": "evidence_span", "required": True},
                    "stance": {"type": "enum", "values": ["affirms", "denies"]},
                    "confidence": {"type": "number", "range": [0, 1]},
                },
            },
            {
                "id": "cross_reference",
                "fields": {
                    "target": {"type": "string"},
                    "quote": {"type": "evidence_span", "required": True},
                },
            },
        ]
    }

    def test_each_record_names_its_own_type(self):
        """`extraction_records.record_type` is a column, so it has to arrive."""
        item = build_output_schema(self.SCHEMA)["properties"]["records"]["items"]
        assert item["required"] == ["record_type", "fields"]
        assert item["properties"]["record_type"]["enum"] == ["claim", "cross_reference"]

    def test_fields_are_a_flat_object(self):
        fields = build_output_schema(self.SCHEMA)["properties"]["records"]["items"][
            "properties"
        ]["fields"]["properties"]
        assert fields["assertion"]["type"] == "string"
        assert fields["quote"]["type"] == "string"
        assert fields["stance"]["enum"] == ["affirms", "denies"]
        assert (fields["confidence"]["minimum"], fields["confidence"]["maximum"]) == (
            0,
            1,
        )

    def test_an_unknown_field_type_becomes_a_string(self):
        """`{"type": "date"}` is not a JSON Schema type.

        Declared types used to pass through verbatim, so a schema naming any
        type the engine did not know produced a document a validating provider
        rejects outright.
        """
        schema = {
            "record_types": [
                {
                    "id": "note",
                    "fields": {
                        "when": {"type": "date"},
                        "quote": {"type": "evidence_span"},
                    },
                }
            ]
        }
        fields = build_output_schema(schema)["properties"]["records"]["items"][
            "properties"
        ]["fields"]["properties"]
        assert fields["when"]["type"] == "string"

    def test_two_record_types_may_share_a_field(self):
        fields = build_output_schema(self.SCHEMA)["properties"]["records"]["items"][
            "properties"
        ]["fields"]["properties"]
        assert fields["quote"]["type"] == "string"

    def test_a_field_declared_two_incompatible_ways_is_rejected(self):
        schema = {
            "record_types": [
                {"id": "a", "fields": {"n": {"type": "number"}, "q": {"type": "evidence_span"}}},
                {"id": "b", "fields": {"n": {"type": "string"}, "q": {"type": "evidence_span"}}},
            ]
        }
        with pytest.raises(ValidationError, match="'n'"):
            build_output_schema(schema)

    def test_a_schema_with_no_record_types_is_rejected(self):
        with pytest.raises(ValidationError):
            build_output_schema({"record_types": []})


class TestEvidenceFieldNames:
    def test_found_by_declared_type_not_by_name(self):
        record_type = {
            "fields": {
                "quote": {"type": "evidence_span"},
                "evidence_of_nothing": {"type": "string"},
            }
        }
        assert evidence_field_names(record_type) == ["quote"]
