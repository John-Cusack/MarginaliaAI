"""Extraction schema parsing and prompt rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import jinja2
import yaml

if TYPE_CHECKING:
    from research_engine.domain.extractions import ExtractionSchema


def parse_schema_yaml(content: str) -> dict[str, Any]:
    """Parse an extraction schema YAML string."""
    return yaml.safe_load(content)


def render_prompt(
    template: str,
    passage_text: str,
    entity_hints: str = "",
    extra_context: dict[str, Any] | None = None,
) -> str:
    """Render a Jinja2 extraction prompt template."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    tmpl = env.from_string(template)
    ctx = {
        "passage_text": passage_text,
        "entity_hints": entity_hints,
        **(extra_context or {}),
    }
    return tmpl.render(**ctx)


def build_output_schema(schema: ExtractionSchema) -> dict[str, Any]:
    """Build a JSON Schema for LLM structured output from an extraction schema."""
    record_types = schema.schema_def.get("record_types", [])
    properties = {}
    for rt in record_types:
        rt_id = rt.get("id", "record")
        fields = rt.get("fields", {})
        field_properties = {}
        required = []
        for fname, fspec in fields.items():
            ftype = fspec.get("type", "string")
            prop: dict[str, Any] = {"description": fspec.get("description", "")}
            if ftype == "evidence_span" or ftype == "entity_ref" or ftype == "fuzzy_date":
                prop["type"] = "string"
            elif ftype == "enum":
                prop["type"] = "string"
                prop["enum"] = fspec.get("values", [])
            elif ftype == "number":
                prop["type"] = "number"
                if r := fspec.get("range"):
                    prop["minimum"] = r[0]
                    prop["maximum"] = r[1]
            else:
                prop["type"] = ftype
            field_properties[fname] = prop
            if fspec.get("required", False):
                required.append(fname)
        properties[rt_id] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": field_properties,
                "required": required,
            },
        }

    return {
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": properties,
                },
            }
        },
        "required": ["records"],
    }
