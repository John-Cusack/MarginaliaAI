"""Extraction schema parsing, prompt rendering, and the LLM output contract.

An extraction schema is authored as YAML: a list of ``record_types``, each with
named ``fields``, plus a Jinja2 ``prompt``. Three things have to agree about the
shape of what comes back from the model — the JSON Schema sent to the provider,
the validator, and the ``extraction_records`` table — and this module is where
that shape is defined once so they cannot drift apart.

The contract is deliberately flat::

    {"records": [{"record_type": "claim", "fields": {"assertion": ..., ...}}]}

Nesting record types as keys reads more naturally, but it loses the one thing
storage needs: ``extraction_records.record_type`` is a column, so each record
has to name its own type. Flat also means the validator can read a record's
fields without knowing which type it is until it looks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import jinja2
import yaml

from research_engine.domain.errors import ValidationError

if TYPE_CHECKING:
    from research_engine.domain.extractions import ExtractionSchema

#: Field type meaning "a verbatim quotation from the passage". Every record type
#: must declare at least one, which is what makes an extracted claim checkable
#: against the corpus rather than merely plausible.
EVIDENCE_TYPE = "evidence_span"

#: Authored field type -> JSON Schema base type. Types absent here used to be
#: passed through verbatim, so a field declared ``type: date`` produced
#: ``{"type": "date"}`` — not a JSON Schema type, and rejected outright by a
#: provider that validates. Anything unlisted is carried as a string.
_JSON_TYPES = {
    "string": "string",
    "text": "string",
    EVIDENCE_TYPE: "string",
    "entity_ref": "string",
    "fuzzy_date": "string",
    "enum": "string",
    "number": "number",
    "integer": "integer",
    "boolean": "boolean",
    "array": "array",
}


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


def record_type_definitions(schema: ExtractionSchema | dict[str, Any]) -> dict[str, dict]:
    """Record type definitions by id, in declaration order."""
    schema_def = schema if isinstance(schema, dict) else schema.schema_def
    definitions: dict[str, dict] = {}
    for record_type in schema_def.get("record_types", []):
        rt_id = record_type.get("id")
        if not rt_id:
            raise ValidationError("Every record_type needs an 'id'.")
        if rt_id in definitions:
            raise ValidationError(f"Record type '{rt_id}' is declared twice.")
        definitions[rt_id] = record_type
    return definitions


def evidence_field_names(record_type: dict) -> list[str]:
    """Fields of this record type declared as verbatim quotations."""
    return [
        name
        for name, spec in record_type.get("fields", {}).items()
        if spec.get("type") == EVIDENCE_TYPE
    ]


def required_field_names(record_type: dict) -> list[str]:
    return [
        name
        for name, spec in record_type.get("fields", {}).items()
        if spec.get("required", False)
    ]


def build_output_schema(schema: ExtractionSchema | dict[str, Any]) -> dict[str, Any]:
    """JSON Schema for the model's structured output.

    Field definitions are merged across record types into one ``fields`` object,
    because a per-type ``oneOf`` is supported unevenly across providers and a
    record that fails provider-side validation comes back as an opaque error.
    Per-type requirements are enforced afterwards, in
    :func:`~research_engine.services.extraction.validation.validate_records`,
    where a violation can name the record and the field it is missing.
    """
    definitions = record_type_definitions(schema)
    if not definitions:
        raise ValidationError("An extraction schema needs at least one record_type.")

    merged: dict[str, dict[str, Any]] = {}
    for rt_id, record_type in definitions.items():
        for name, spec in record_type.get("fields", {}).items():
            candidate = _field_json_schema(spec)
            if name in merged:
                merged[name] = _merge_field(name, rt_id, merged[name], candidate)
            else:
                merged[name] = candidate

    return {
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "description": (
                    "One entry per extracted record. Return an empty array when "
                    "the passage supports none."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "record_type": {
                            "type": "string",
                            "enum": list(definitions),
                            "description": "Which record type this entry is.",
                        },
                        "fields": {
                            "type": "object",
                            "description": (
                                "The fields declared by this record's type. Omit "
                                "fields belonging to other types."
                            ),
                            "properties": merged,
                        },
                    },
                    "required": ["record_type", "fields"],
                },
            }
        },
        "required": ["records"],
    }


def _field_json_schema(spec: dict[str, Any]) -> dict[str, Any]:
    declared = spec.get("type", "string")
    prop: dict[str, Any] = {
        "type": _JSON_TYPES.get(declared, "string"),
        "description": spec.get("description", ""),
    }
    if declared == EVIDENCE_TYPE and not prop["description"]:
        prop["description"] = "Quote the passage exactly, word for word."
    if declared == "enum":
        prop["enum"] = list(spec.get("values", []))
    if declared == "number" and (bounds := spec.get("range")):
        prop["minimum"], prop["maximum"] = bounds[0], bounds[1]
    if declared == "array":
        prop["items"] = {"type": "string"}
    return prop


def _merge_field(
    name: str, record_type: str, existing: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Reconcile one field name declared by two record types.

    Widening an enum or a numeric range is safe — the per-type check downstream
    still holds each record to its own type's declaration. Disagreeing on the
    base type is not recoverable, and guessing would send the model a shape that
    contradicts half the schema.
    """
    if existing["type"] != candidate["type"]:
        raise ValidationError(
            f"Field '{name}' is declared as {existing['type']} by one record "
            f"type and {candidate['type']} by '{record_type}'. Give them "
            f"different names, or the same type."
        )
    merged = dict(existing)
    if "enum" in existing or "enum" in candidate:
        values = list(existing.get("enum", []))
        values += [v for v in candidate.get("enum", []) if v not in values]
        merged["enum"] = values
    if "minimum" in existing and "minimum" in candidate:
        merged["minimum"] = min(existing["minimum"], candidate["minimum"])
        merged["maximum"] = max(existing["maximum"], candidate["maximum"])
    return merged
